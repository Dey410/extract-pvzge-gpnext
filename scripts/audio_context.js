// iOS WebKit uses the AmbientSound audio session while a page plays only Web
// Audio and MediaPlayback while an <audio> element plays. With music moved off
// <audio>, an iPad came back from the background with the AudioContext
// "running" but its clock stopped. Requesting the playback session keeps the
// category the game had with DOM music; it must be set before the engine
// creates its AudioContext.
function __pvzgeUsePlaybackAudioSession() {
  "use strict";
  var session = typeof navigator !== "undefined" ? navigator.audioSession : undefined;
  if (!session) {
    console.warn("[pvzge-audio] audio session API unavailable");
    return;
  }
  session.type = "playback";
  console.warn("[pvzge-audio] audio session type=" + session.type);
}

// Logs the AudioContext state when the game is shown again, and whether its
// clock advances a second later.
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
