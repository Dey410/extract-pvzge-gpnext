// iOS WebKit can leave an AudioContext silent after the app returns from the
// background: stuck "interrupted", or "running" with a clock that does not
// advance (WebKit bugs 263627, 283419, 291892). Suspending the context while
// the game is hidden and cycling suspend/resume when it is shown restarts the
// audio output. The logged state and clock show whether it recovered.
function __pvzgeRestartAudioContextOnShow(deps) {
  "use strict";
  if (!deps.agent) return;
  var context = deps.agent._context;
  function report(label) {
    console.warn("[pvzge-audio] " + label + " state=" + context.state + " time=" + context.currentTime.toFixed(3));
  }
  deps.bus.on(deps.busEvents.EVENT_HIDE, function () {
    context.suspend().catch(function (error) { console.error("[pvzge-audio] suspend failed", error); });
  });
  deps.bus.on(deps.busEvents.EVENT_SHOW, function () {
    report("show");
    context.suspend().then(function () { return context.resume(); }).then(function () {
      var time = context.currentTime;
      setTimeout(function () { report(context.currentTime > time ? "resumed" : "resumed without progress"); }, 500);
    }).catch(function (error) { console.error("[pvzge-audio] resume failed", error); });
  });
}
