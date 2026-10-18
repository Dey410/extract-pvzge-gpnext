// Logs the AudioContext state when the game is shown again and whether its
// clock advances a second later. On the iPad host the context has come back
// "interrupted", or "running" with a stopped clock, after an app switch.
function __pvzgeReportAudioContextOnShow(deps) {
  "use strict";
  if (!deps.agent) return;
  var context = deps.agent._context;
  function report(label) {
    console.warn("[pvzge-audio] " + label + " state=" + context.state + " time=" + context.currentTime.toFixed(3));
  }
  deps.bus.on(deps.busEvents.EVENT_SHOW, function () {
    var time = context.currentTime;
    report("show");
    setTimeout(function () { report(context.currentTime > time ? "advancing" : "stalled"); }, 1000);
  });
}
