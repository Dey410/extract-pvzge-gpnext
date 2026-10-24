// Web Audio stand-in for the game's window.audioEngine, the pool of <audio>
// elements behind TauriAudioAPI, with the same methods and voice limit.
// A clip is fetched and decoded when it first plays and shared by URL;
// released clips stay in a cache bounded in decoded bytes.
// A voice's clock runs from the play request at its rate, so the game, which
// switches music on getPosition(), stays on time while a clip decodes or after
// one fails to decode. Volume and fades use gain, which iPhone WebKit honours
// where it ignores media element volume.
function __pvzgeCreateAudioEngine(sampleRate, cacheBytes) {
  "use strict";
  var VOICES = 60;
  var DEFAULT_VOLUME = 0.9;

  function AudioEngine(manifest, audioRoot) {
    this.audioRoot = audioRoot;
    this.manifest = manifest;
    this.context = null;
    this.nextId = 1;
    this.voices = new Map();
    // url -> {url, buffer, pending, refs, bytes}; idle holds unreferenced
    // decoded clips in least-recently-released order.
    this.clips = new Map();
    this.idle = new Map();
    this.idleBytes = 0;
  }

  var p = AudioEngine.prototype;

  p.init = function () {
    var self = this;
    // On iOS WebKit's default ("auto") session leaves this engine silent: the
    // clips play but nothing is heard. Asking for playback, as the <audio> pool
    // had, makes it audible in loaders that do not ask for it themselves.
    var session = window.navigator.audioSession;
    if (session) session.type = "playback";
    this.context = new (window.AudioContext || window.webkitAudioContext)({sampleRate: sampleRate});
    // The context may start suspended until a gesture; any later tap or key
    // resumes it again.
    ["touchend", "mouseup", "keydown"].forEach(function (type) {
      window.addEventListener(type, function () { self._wake(); }, true);
    });
    return Promise.resolve();
  };

  p._wake = function () {
    var context = this.context;
    if (context.state === "running") return;
    context.resume().catch(function (error) { console.warn("[pvzge-audio] resume failed", error); });
  };

  p.nameToPath = function (name) {
    return this.manifest[name] ?? null;
  };

  p._park = function (clip) {
    if (clip.bytes > cacheBytes) {
      this.clips.delete(clip.url);
      return;
    }
    this.idle.set(clip.url, clip);
    this.idleBytes += clip.bytes;
    while (this.idleBytes > cacheBytes) {
      var oldest = this.idle.values().next().value;
      this.idle.delete(oldest.url);
      this.idleBytes -= oldest.bytes;
      this.clips.delete(oldest.url);
    }
  };

  p._acquire = function (url) {
    var self = this, clip = this.clips.get(url);
    if (!clip) {
      clip = {url: url, buffer: null, pending: null, refs: 0, bytes: 0};
      this.clips.set(url, clip);
    }
    if (clip.refs === 0 && this.idle.delete(url)) this.idleBytes -= clip.bytes;
    clip.refs++;
    if (clip.buffer) return Promise.resolve(clip.buffer);
    if (!clip.pending) {
      clip.pending = fetch(url).then(function (response) {
        if (!response.ok) throw new Error("load audio failed: " + url + ", status " + response.status);
        return response.arrayBuffer();
      }).then(function (data) {
        return self.context.decodeAudioData(data);
      }).then(function (buffer) {
        clip.pending = null;
        clip.buffer = buffer;
        clip.bytes = buffer.length * buffer.numberOfChannels * 4;
        if (clip.refs === 0) self._park(clip);
        return buffer;
      }, function (error) {
        clip.pending = null;
        if (clip.refs === 0) self.clips.delete(url);
        throw error;
      });
    }
    return clip.pending;
  };

  p._releaseClip = function (url) {
    var clip = this.clips.get(url);
    clip.refs--;
    if (clip.refs > 0) return;
    if (clip.buffer) this._park(clip);
    else if (!clip.pending) this.clips.delete(url);
  };

  // Media seconds, held inside the clip once its length is known; a voice with
  // restartBeforeEnd wraps at its loop end like the pool's restart timer.
  p._position = function (voice) {
    var position = voice.offset;
    if (voice.ticking) position += (this.context.currentTime - voice.anchor) * voice.speed;
    var buffer = voice.buffer;
    if (buffer && buffer.duration > 0) {
      var end = this._loopEnd(voice);
      position = end === null ? Math.min(position, buffer.duration) : position % end;
    }
    return position;
  };

  p._loopEnd = function (voice) {
    if (voice.restartBeforeEnd === null || !voice.buffer) return null;
    var end = voice.buffer.duration - voice.restartBeforeEnd / voice.speed;
    return end > 0 ? end : voice.buffer.duration;
  };

  p._rebase = function (voice) {
    voice.offset = this._position(voice);
    voice.anchor = this.context.currentTime;
  };

  p._startSource = function (voice) {
    var self = this, buffer = voice.buffer;
    var position = this._position(voice);
    var end = this._loopEnd(voice);
    if (end === null && position >= buffer.duration) {
      this._release(voice);
      return;
    }
    var source = this.context.createBufferSource();
    source.buffer = buffer;
    source.playbackRate.value = voice.speed;
    if (end !== null) {
      source.loop = true;
      source.loopEnd = end;
    }
    source.connect(voice.gain);
    source.onended = function () {
      if (voice.source !== source) return;
      voice.source = null;
      self._release(voice);
    };
    source.start(0, position);
    voice.source = source;
  };

  p._stopSource = function (voice) {
    var source = voice.source;
    if (!source) return;
    voice.source = null;
    source.onended = null;
    try { source.stop(); } catch (_) { /* A source that already ended cannot stop again. */ }
    source.disconnect();
  };

  p._release = function (voice) {
    if (!this.voices.delete(voice.id)) return;
    clearTimeout(voice.fadeTimer);
    this._stopSource(voice);
    voice.gain.disconnect();
    voice.buffer = null;
    voice.ticket++;
    this._releaseClip(voice.url);
  };

  p.play = function (name, speed, volume) {
    if (speed === void 0) speed = 1;
    if (volume === void 0) volume = DEFAULT_VOLUME;
    if (this.voices.size >= VOICES) {
      console.warn("[audio] pool exhausted, drop:", name);
      return null;
    }
    var path = this.nameToPath(name);
    if (!path) {
      console.warn("[audio] unknown clip:", name);
      return null;
    }
    var self = this, context = this.context;
    var url = new URL(path.split("/").map(encodeURIComponent).join("/"), this.audioRoot).href;
    var gain = context.createGain();
    gain.gain.value = Math.min(1, Math.max(0, volume));
    gain.connect(context.destination);
    var voice = {
      id: this.nextId++, url: url, speed: speed, volume: gain.gain.value, gain: gain,
      buffer: null, source: null, ticking: true, offset: 0, anchor: context.currentTime,
      restartBeforeEnd: null, fading: false, fadeTimer: null, ticket: 0
    };
    this.voices.set(voice.id, voice);
    this._wake();
    var ticket = voice.ticket;
    this._acquire(url).then(function (buffer) {
      if (ticket !== voice.ticket) return;
      voice.buffer = buffer;
      if (voice.ticking) self._startSource(voice);
    }, function (error) {
      if (ticket !== voice.ticket) return;
      console.error("[audio] decode failed:", url, error);
    });
    return voice.id;
  };

  p.setVolume = function (id, volume) {
    var voice = this.voices.get(id);
    if (!voice) return;
    voice.volume = Math.min(1, Math.max(0, volume));
    if (!voice.fading) voice.gain.gain.value = voice.volume;
  };

  p.setPlaying = function (id, playing) {
    var voice = this.voices.get(id);
    if (!voice || voice.ticking === !!playing) return;
    this._rebase(voice);
    voice.ticking = !!playing;
    if (!playing) this._stopSource(voice);
    else {
      this._wake();
      if (voice.buffer) this._startSource(voice);
    }
  };

  p.seek = function (id, seconds) {
    var voice = this.voices.get(id);
    if (!voice) return;
    var position = Math.max(0, seconds);
    voice.offset = voice.buffer ? Math.min(position, voice.buffer.duration) : position;
    voice.anchor = this.context.currentTime;
    if (voice.source) {
      this._stopSource(voice);
      this._startSource(voice);
    }
  };

  p.getPosition = function (id) {
    var voice = this.voices.get(id);
    if (!voice) return [0, null];
    return [this._position(voice), voice.buffer ? voice.buffer.duration : null];
  };

  p.getDuration = function (id) {
    var voice = this.voices.get(id);
    return voice && voice.buffer ? voice.buffer.duration : null;
  };

  p.fadeOut = function (id, durationMs) {
    var self = this, voice = this.voices.get(id);
    if (!voice || voice.fading) return;
    voice.fading = true;
    var now = this.context.currentTime, gain = voice.gain.gain;
    gain.cancelScheduledValues(now);
    gain.setValueAtTime(gain.value, now);
    gain.linearRampToValueAtTime(0, now + durationMs / 1000);
    voice.fadeTimer = setTimeout(function () { self.destroy(id); }, durationMs);
  };

  p.setRestartBeforeEnd = function (id, beforeMs) {
    var voice = this.voices.get(id);
    if (!voice) return;
    this._rebase(voice);
    voice.restartBeforeEnd = beforeMs / 1000;
    if (voice.source) {
      this._stopSource(voice);
      this._startSource(voice);
    }
  };

  p.destroy = function (id) {
    var voice = this.voices.get(id);
    if (voice) this._release(voice);
  };

  // Finished voices are released when they end, so nothing is left to clean.
  p.cleanupFinished = function () {
    return 0;
  };

  p.listClips = function () {
    return Object.keys(this.manifest);
  };

  return AudioEngine;
}
