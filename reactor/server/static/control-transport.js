/** HTTP and WebSocket ownership for the reactor control page. */
export const SHUTDOWN_HINT = "Stops this server and any other reactor server still "
  + "running, then start it again from the shortcut. Aborts a running recipe, "
  + "and gas stops either way — read the prompt.";

export function createControlTransport({$, document, fetchImpl, WebSocketCtor,
                                        location, render, setTimeoutImpl=setTimeout,
                                        clearTimeoutImpl=clearTimeout,
                                        reconnectDelayMs=1500, sessionStorage=null,
                                        reloadPage=()=>{}, onLifecycle=()=>{},
                                        restartTimeoutMs=30000,
                                        shutdownTimeoutMs=8000,
                                        requestTimeoutMs=2000,
                                        nowImpl=()=>globalThis.performance?.now?.() ?? Date.now(),
                                        AbortControllerCtor=globalThis.AbortController}) {
  let socket = null;
  let reconnectTimer = null;
  let toastTimer = null;
  let suspended = false;
  let disposed = false;
  let lifecycleActive = false;

  function setLink(text, kind){
    const badge = $("linkBadge");
    badge.textContent = text;
    badge.className = "badge " + (kind || "");
  }

  function toast(msg, ok){
    document.querySelectorAll(".toast").forEach(t => t.remove());
    const el = document.createElement("div");
    el.className = "toast" + (ok ? " ok" : "");
    el.textContent = msg;
    document.body.appendChild(el);
    if(toastTimer !== null) clearTimeoutImpl(toastTimer);
    toastTimer = setTimeoutImpl(() => {
      toastTimer = null;
      el.remove();
    }, ok ? 2500 : 7000);
  }

  async function post(url, body){
    let response;
    try{
      response = await fetchImpl(url, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: body === undefined ? "{}" : JSON.stringify(body)
      });
    }catch(error){
      const msg = `Server request failed: ${error && error.message ? error.message : "disconnected"}`;
      toast(msg);
      throw new Error(msg);
    }
    if(!response.ok){
      let msg = response.statusText;
      try { msg = (await response.json()).detail || msg; } catch(_){}
      toast(msg);
      throw new Error(msg);
    }
    return response.json();
  }

  function connect(){
    if(disposed || suspended) return null;
    if(socket) return socket;
    if(reconnectTimer !== null){
      clearTimeoutImpl(reconnectTimer);
      reconnectTimer = null;
    }
    const ws = new WebSocketCtor(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
    socket = ws;
    ws.onopen = () => {
      if(disposed || suspended || socket !== ws) return;
      setLink("live", "live");
      const button = $("shutdownBtn"), restart = $("restartBtn"), hint = $("shutdownHint");
      if(!lifecycleActive && button && button.disabled){
        button.disabled = false;
        if(restart) restart.disabled = false;
        if(hint) hint.textContent = SHUTDOWN_HINT;
      }
    };
    ws.onclose = () => {
      if(disposed || socket !== ws) return;
      socket = null;
      setLink("disconnected", "bad");
      reconnectTimer = setTimeoutImpl(() => {
        reconnectTimer = null;
        connect();
      }, reconnectDelayMs);
    };
    ws.onerror = () => {
      if(!disposed && !suspended && socket === ws) setLink("error", "bad");
    };
    ws.onmessage = ev => {
      if(!disposed && !suspended && socket === ws) render(JSON.parse(ev.data));
    };
    return ws;
  }

  function stopSocket(){
    if(reconnectTimer !== null) clearTimeoutImpl(reconnectTimer);
    reconnectTimer = null;
    const ws = socket;
    socket = null;
    if(ws){
      ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
      if(typeof ws.close === "function") ws.close();
    }
  }


  let shutdownGeneration = 0, shutdownTimer = null, shutdownWake = null;
  let shutdownController = null;
  function shutdownDelay(){
    return new Promise(resolve => {
      shutdownWake = resolve;
      shutdownTimer = setTimeoutImpl(() => {shutdownTimer=null; shutdownWake=null; resolve();},250);
    });
  }
  function cancelShutdownWatch(){
    shutdownGeneration++;
    if(shutdownTimer !== null) clearTimeoutImpl(shutdownTimer);
    shutdownTimer = null;
    if(shutdownWake) shutdownWake();
    shutdownWake = null;
    if(shutdownController) shutdownController.abort();
    shutdownController = null;
    lifecycleActive = false;
  }

  let restartGeneration = 0, restartTimer = null, restartWake = null;
  let restartController = null;
  function restartDelay(){
    return new Promise(resolve => {
      restartWake = resolve;
      restartTimer = setTimeoutImpl(() => {restartTimer=null; restartWake=null; resolve();}, 250);
    });
  }
  function cancelRestartWatch(){
    restartGeneration++;
    if(restartTimer !== null) clearTimeoutImpl(restartTimer);
    restartTimer = null;
    if(restartWake) restartWake();
    restartWake = null;
    if(restartController) restartController.abort();
    restartController = null;
    lifecycleActive = false;
  }

  async function lifecycleFetch(url, deadline, kind, parseJson=false){
    const remaining = deadline - nowImpl();
    if(remaining <= 0) throw Object.assign(new Error("lifecycle deadline expired"), {timedOut:true});
    const controller = AbortControllerCtor ? new AbortControllerCtor() : null;
    if(kind === "shutdown") shutdownController = controller;
    else restartController = controller;
    let timer = null;
    const timeoutMs = Math.min(requestTimeoutMs, remaining);
    const timeout = new Promise((_, reject) => {
      timer = setTimeoutImpl(() => {
        if(controller) controller.abort();
        reject(Object.assign(new Error("request timed out"), {timedOut:true}));
      }, timeoutMs);
    });
    const request = (async () => {
      const response = await fetchImpl(url, {cache:"no-store",
        ...(controller ? {signal:controller.signal} : {})});
      const body = parseJson && response.ok ? await response.json() : null;
      return {response, body};
    })();
    request.catch(() => {}); // a transport that ignores AbortSignal may settle late
    try{
      return await Promise.race([request, timeout]);
    }finally{
      if(timer !== null) clearTimeoutImpl(timer);
      if(kind === "shutdown" && shutdownController === controller) shutdownController = null;
      if(kind === "restart" && restartController === controller) restartController = null;
    }
  }

  async function watchShutdown(receipt){
    cancelShutdownWatch();
    const generation = ++shutdownGeneration;
    lifecycleActive = true;
    const hint = $("shutdownHint");
    const released = (receipt && receipt.released) || [];
    const failed   = (receipt && receipt.failed)   || [];
    const killed   = (receipt && receipt.also_killed) || [];

    // What was released, stated before the process is even gone.
    const parts = [];
    if(released.length) parts.push(`Released ${released.join(", ")}`);
    if(killed.length)   parts.push(`killed ${killed.length} other instance(s)`);
    const summary = parts.join(" · ");

    const deadline = nowImpl() + shutdownTimeoutMs;
    while(nowImpl() < deadline){
      await shutdownDelay();
      if(disposed || suspended || generation !== shutdownGeneration) return;
      let alive = false;
      try{
        // no-store: a cached 200 would read as "still up" forever.
        const {response} = await lifecycleFetch("/api/state", deadline, "shutdown");
        alive = response.ok;
      }catch(error){
        if(error && error.timedOut) continue;
        alive = false;
      }
      if(disposed || suspended || generation !== shutdownGeneration) return;
      if(!alive){
        if(failed.length){
          // Released most of it, but not all - name what did not let go, because
          // that is exactly what the next server will fail to open.
          hint.textContent = `Server stopped, but ${failed.length} teardown step`
            + `(s) failed: ${failed.map(f => f.what).join(", ")}. `
            + `${summary}. Check server.log before starting again.`;
          hint.style.color = "var(--warn)";
          toast("Server stopped, but not everything was released", true);
        }else{
          hint.textContent = `✓ Stopped and released — ready to start again from `
            + `the Reactor Interface shortcut.${summary ? "  " + summary + "." : ""}`;
          hint.style.color = "var(--ok)";
        }
        lifecycleActive = false;
        return;
      }
    }
    if(disposed || suspended || generation !== shutdownGeneration) return;
    lifecycleActive = false;
    $("shutdownBtn").disabled = false;
    const restart = $("restartBtn");
    if(restart) restart.disabled = false;
    hint.textContent = "Shutdown could not be confirmed before the browser deadline. "
      + (failed.length ? `${failed.length} teardown step(s) also reported failure. ` : "")
      + "The saved receipt remains authoritative; check server.log before starting again.";
    hint.style.color = "var(--bad)";
    toast("Shutdown could not be confirmed");
  }

  async function watchRestart(receipt){
    cancelRestartWatch();
    const generation = ++restartGeneration;
    lifecycleActive = true;
    const hint = $("shutdownHint");
    const restart = receipt && receipt.restart;
    const replaced = restart && restart.replaces_instance_id;
    if(!replaced){
      hint.textContent = "Restart did not receive a replacement-server identity.";
      hint.style.color = "var(--bad)";
      onLifecycle("error", "restart failed: no replacement-server identity was returned");
      $("shutdownBtn").disabled = false;
      $("restartBtn").disabled = false;
      toast("Restart could not be confirmed");
      lifecycleActive = false;
      return;
    }
    const deadline = nowImpl() + restartTimeoutMs;
    let oldServerGone = false, lastRemaining = null;
    while(nowImpl() < deadline){
      await restartDelay();
      if(disposed || suspended || generation !== restartGeneration) return;
      try{
        const {response, body:identity} = await lifecycleFetch(
          "/api/server/version", deadline, "restart", true);
        if(identity && identity.instance_id && identity.instance_id !== replaced){
          const version = identity.version || restart.version || "unknown";
          if(sessionStorage && typeof sessionStorage.setItem === "function")
            sessionStorage.setItem("reactor.restart.success", version);
          reloadPage();       // same tab: refreshes the replacement's assets and state
          lifecycleActive = false;
          return;
        }
      }catch(_){
        if(!oldServerGone){
          oldServerGone = true;
          onLifecycle("restart", "previous server disconnected; waiting for replacement startup");
        }
      }                       // expected while the old server exits and replacement starts
      const remaining = Math.max(1, Math.ceil((deadline - nowImpl()) / 1000));
      if(remaining !== lastRemaining){
        lastRemaining = remaining;
        hint.textContent = `Hardware released. Waiting for the replacement server `
          + `(${remaining} s before this is declared failed)…`;
      }
    }
    if(disposed || suspended || generation !== restartGeneration) return;
    lifecycleActive = false;
    hint.textContent = "Restart failed: the replacement server did not become reachable. "
      + "Start it from the Reactor Interface shortcut; the failure remains in Error Log.";
    hint.style.color = "var(--bad)";
    $("shutdownBtn").disabled = false;
    $("restartBtn").disabled = false;
    onLifecycle("error", `restart failed: replacement server was not reachable within `
      + `${Math.ceil(restartTimeoutMs / 1000)} seconds`);
    toast("Restart could not be confirmed");
  }

  function suspend(){
    if(disposed) return;
    suspended = true;
    cancelShutdownWatch();
    cancelRestartWatch();
    stopSocket();
  }

  function resume(connectNow=true){
    if(disposed) return null;
    suspended = false;
    return connectNow ? connect() : null;
  }

  function dispose(){
    if(disposed) return;
    disposed = true;
    suspended = true;
    cancelShutdownWatch();
    cancelRestartWatch();
    stopSocket();
    if(toastTimer !== null) clearTimeoutImpl(toastTimer);
    toastTimer = null;
  }

  return {connect, dispose, post, resume, setLink, suspend, toast, watchShutdown, watchRestart};
}
