/** HTTP and WebSocket ownership for the reactor control page. */
export const SHUTDOWN_HINT = "Stops this server and any other reactor server still "
  + "running, then start it again from the shortcut. Aborts a running recipe, "
  + "and gas stops either way — read the prompt.";

export function createControlTransport({$, document, fetchImpl, WebSocketCtor,
                                        location, render, setTimeoutImpl=setTimeout,
                                        clearTimeoutImpl=clearTimeout,
                                        reconnectDelayMs=1500}) {
  let socket = null;
  let reconnectTimer = null;
  let toastTimer = null;
  let suspended = false;
  let disposed = false;

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
    const response = await fetchImpl(url, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: body === undefined ? "{}" : JSON.stringify(body)
    });
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
      const button = $("shutdownBtn"), hint = $("shutdownHint");
      if(button && button.disabled){
        button.disabled = false;
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
  }

  async function watchShutdown(receipt){
    const generation = ++shutdownGeneration;
    const hint = $("shutdownHint");
    const released = (receipt && receipt.released) || [];
    const failed   = (receipt && receipt.failed)   || [];
    const killed   = (receipt && receipt.also_killed) || [];

    // What was released, stated before the process is even gone.
    const parts = [];
    if(released.length) parts.push(`Released ${released.join(", ")}`);
    if(killed.length)   parts.push(`killed ${killed.length} other instance(s)`);
    const summary = parts.join(" · ");

    const deadline = Date.now() + 8000;    // the server's own fallback is 3 s
    while(Date.now() < deadline){
      await shutdownDelay();
      if(disposed || suspended || generation !== shutdownGeneration) return;
      let alive = false;
      try{
        // no-store: a cached 200 would read as "still up" forever.
        const r = await fetchImpl("/api/state", {cache: "no-store"});
        alive = r.ok;
      }catch(_){ alive = false; }
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
        return;
      }
    }
    if(disposed || suspended || generation !== shutdownGeneration) return;
    // Still answering past the server's own hard deadline. The teardown receipt
    // above still stands - the devices ARE released - but the process is lingering,
    // and a lingering process still owns port 8000.
    $("shutdownBtn").disabled = false;
    hint.textContent = (failed.length ? "Some teardown steps failed, and the PROCESS is still running " : "Devices were released, but the PROCESS is still running ")
      + "— it still holds port 8000, so starting from the shortcut will fail to "
      + "bind. End it from Task Manager (pythonw.exe), then start again.";
    hint.style.color = "var(--bad)";
    toast("Shutdown released the hardware but the process did not exit");
  }

  function suspend(){
    if(disposed) return;
    suspended = true;
    cancelShutdownWatch();
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
    stopSocket();
    if(toastTimer !== null) clearTimeoutImpl(toastTimer);
    toastTimer = null;
  }

  return {connect, dispose, post, resume, setLink, suspend, toast, watchShutdown};
}
