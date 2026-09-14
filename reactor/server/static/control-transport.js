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

  function suspend(){
    if(disposed) return;
    suspended = true;
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
    stopSocket();
    if(toastTimer !== null) clearTimeoutImpl(toastTimer);
    toastTimer = null;
  }

  return {connect, dispose, post, resume, setLink, suspend, toast};
}
