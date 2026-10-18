// Cocos 3.8.4 audio player for music AudioClips. Loading a clip reads and
// decodes nothing; a track is decoded when it first plays and shared by URL.
// The play clock runs from the play request at the playback rate, so the game,
// which switches tracks on currentTime, stays on time while a track decodes
// or after one fails to decode.
function __pvzgeCreateMusicPlayer(getDeps, cacheBytes) {
  "use strict";
  var deps = null;
  function engine() { return deps || (deps = getDeps()); }

  // url -> {url, buffer, pending, refs, bytes}; idle holds unreferenced
  // decoded tracks in least-recently-released order.
  var tracks = new Map();
  var idle = new Map();
  var idleBytes = 0;

  function fetchAndDecode(url) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      function fail(reason) { reject(new Error("load music failed: " + url + ", " + reason)); }
      xhr.open("GET", url, true);
      xhr.responseType = "arraybuffer";
      xhr.onload = function () {
        if (xhr.status === 200 || xhr.status === 0) resolve(xhr.response);
        else fail("status " + xhr.status);
      };
      xhr.onerror = function () { fail("error"); };
      xhr.ontimeout = function () { fail("timeout"); };
      xhr.onabort = function () { fail("abort"); };
      xhr.send(null);
    }).then(function (data) { return engine().agent.decodeAudioData(data); });
  }

  function park(track) {
    idle.set(track.url, track);
    idleBytes += track.bytes;
    while (idleBytes > cacheBytes) {
      var oldest = idle.values().next().value;
      idle.delete(oldest.url);
      idleBytes -= oldest.bytes;
      tracks.delete(oldest.url);
    }
  }

  function acquire(url) {
    var track = tracks.get(url);
    if (!track) {
      track = {url: url, buffer: null, pending: null, refs: 0, bytes: 0};
      tracks.set(url, track);
    }
    if (track.refs === 0 && idle.delete(url)) idleBytes -= track.bytes;
    track.refs++;
    if (track.buffer) return Promise.resolve(track.buffer);
    if (!track.pending) {
      track.pending = fetchAndDecode(url).then(function (buffer) {
        track.pending = null;
        track.buffer = buffer;
        track.bytes = buffer.length * buffer.numberOfChannels * 4;
        if (track.refs === 0) park(track);
        return buffer;
      }, function (error) {
        track.pending = null;
        if (track.refs === 0) tracks.delete(url);
        throw error;
      });
    }
    return track.pending;
  }

  function release(url) {
    var track = tracks.get(url);
    track.refs--;
    if (track.refs > 0) return;
    if (track.buffer) park(track);
    else if (!track.pending) tracks.delete(url);
  }

  function MusicPlayer(url) {
    var e = engine();
    this._url = url;
    this._state = e.states.INIT;
    this._listeners = {};
    this._gain = null;
    this._source = null;
    this._buffer = null;
    this._holding = false;
    this._ticket = 0;
    this._waiting = null;
    this._volume = 1;
    this._loop = false;
    this._rate = 1;
    this._offset = 0;
    this._anchor = 0;
    this._ticking = false;
    e.bus.on(e.busEvents.EVENT_PAUSE, this._onInterruptedBegin, this);
    e.bus.on(e.busEvents.EVENT_RESUME, this._onInterruptedEnd, this);
  }

  MusicPlayer.load = function (url) { return Promise.resolve(new MusicPlayer(url)); };

  var p = MusicPlayer.prototype;

  p._emit = function (type) {
    (this._listeners[type] || []).slice().forEach(function (listener) { listener(); });
  };
  p._on = function (type, listener) { (this._listeners[type] = this._listeners[type] || []).push(listener); };
  p._off = function (type, listener) {
    if (!listener) delete this._listeners[type];
    else this._listeners[type] = (this._listeners[type] || []).filter(function (item) { return item !== listener; });
  };
  p.onEnded = function (listener) { this._on(engine().events.ENDED, listener); };
  p.offEnded = function (listener) { this._off(engine().events.ENDED, listener); };
  p.onInterruptionBegin = function (listener) { this._on(engine().events.INTERRUPTION_BEGIN, listener); };
  p.offInterruptionBegin = function (listener) { this._off(engine().events.INTERRUPTION_BEGIN, listener); };
  p.onInterruptionEnd = function (listener) { this._on(engine().events.INTERRUPTION_END, listener); };
  p.offInterruptionEnd = function (listener) { this._off(engine().events.INTERRUPTION_END, listener); };

  p._position = function () {
    var position = this._offset;
    if (this._ticking) position += (engine().agent.currentTime - this._anchor) * this._rate;
    var buffer = this._buffer;
    if (buffer && buffer.duration > 0) {
      position = this._loop ? position % buffer.duration : Math.min(position, buffer.duration);
    }
    return position;
  };

  p._rebase = function () {
    this._offset = this._position();
    this._anchor = engine().agent.currentTime;
  };

  p._acquire = function () {
    if (this._holding) return;
    this._holding = true;
    var self = this, ticket = ++this._ticket, url = this._url;
    acquire(url).then(function (buffer) {
      if (ticket !== self._ticket) return;
      self._buffer = buffer;
      if (self._state === engine().states.PLAYING && !self._source) self._startSource();
    }, function (error) {
      if (ticket !== self._ticket) return;
      console.error("music playback failed", url, error);
    });
  };

  p._release = function () {
    if (!this._holding) return;
    this._holding = false;
    this._ticket++;
    this._buffer = null;
    release(this._url);
  };

  p._startSource = function () {
    var e = engine(), self = this, buffer = this._buffer;
    var position = this._position();
    if (!this._loop && position >= buffer.duration) { this._finish(); return; }
    var source = e.agent.createBufferSource(buffer, this._loop);
    source.playbackRate.value = this._rate;
    source.connect(this._gain);
    source.onended = function () {
      if (self._source !== source) return;
      self._source = null;
      self._finish();
    };
    source.start(0, position);
    this._source = source;
  };

  p._stopSource = function () {
    var source = this._source;
    if (!source) return;
    this._source = null;
    source.onended = null;
    try { source.stop(); } catch (_) { /* A source that already ended cannot stop again. */ }
    source.disconnect();
    source.buffer = null;
  };

  p._finish = function () {
    this._ticking = false;
    this._offset = 0;
    this._state = engine().states.INIT;
    this._emit(engine().events.ENDED);
  };

  p._cancelWaiting = function () {
    if (!this._waiting) return;
    engine().agent.offRunning(this._waiting);
    this._waiting = null;
  };

  p._start = function () {
    var e = engine();
    this._stopSource();
    this._rebase();
    this._ticking = true;
    this._state = e.states.PLAYING;
    if (!this._gain) {
      this._gain = e.agent.createGain(this._volume);
      e.agent.connectContext(this._gain);
    }
    if (this._buffer) this._startSource();
    else this._acquire();
  };

  p.play = function () {
    var self = this, agent = engine().agent;
    this._cancelWaiting();
    return new Promise(function (resolve) {
      if (agent.isRunning) { self._start(); resolve(); return; }
      self._waiting = function () { self._waiting = null; self._start(); resolve(); };
      agent.onceRunning(self._waiting);
      agent.runContext().catch(function (error) { console.error("music context resume failed", error); });
    });
  };

  p.pause = function () {
    this._cancelWaiting();
    if (this._state !== engine().states.PLAYING) return Promise.resolve();
    this._rebase();
    this._ticking = false;
    this._stopSource();
    this._state = engine().states.PAUSED;
    return Promise.resolve();
  };

  p.stop = function () {
    this._cancelWaiting();
    this._stopSource();
    this._ticking = false;
    this._offset = 0;
    this._state = engine().states.STOPPED;
    this._release();
    return Promise.resolve();
  };

  p.seek = function (time) {
    this._offset = Math.max(0, Number(time) || 0);
    this._anchor = engine().agent.currentTime;
    if (this._source) {
      this._stopSource();
      this._startSource();
    }
    return Promise.resolve();
  };

  p.destroy = function () {
    var e = engine();
    this._cancelWaiting();
    this._stopSource();
    this._ticking = false;
    this._release();
    if (this._gain) {
      this._gain.disconnect();
      this._gain = null;
    }
    e.bus.off(e.busEvents.EVENT_PAUSE, this._onInterruptedBegin, this);
    e.bus.off(e.busEvents.EVENT_RESUME, this._onInterruptedEnd, this);
    this._listeners = {};
  };

  p.getPCMData = function () {};

  p._onInterruptedBegin = function () {
    var self = this, e = engine();
    if (this._state !== e.states.PLAYING) return;
    this.pause().then(function () {
      self._state = e.states.INTERRUPTED;
      self._emit(e.events.INTERRUPTION_BEGIN);
    });
  };

  p._onInterruptedEnd = function () {
    var self = this, e = engine();
    if (this._state !== e.states.INTERRUPTED) return;
    this.play().then(function () { self._emit(e.events.INTERRUPTION_END); });
  };

  Object.defineProperties(p, {
    src: {get: function () { return this._url; }},
    type: {get: function () { return engine().types.WEB_AUDIO; }},
    state: {get: function () { return this._state; }},
    duration: {get: function () { return this._buffer ? this._buffer.duration : 0; }},
    sampleRate: {get: function () { return this._buffer ? this._buffer.sampleRate : 0; }},
    currentTime: {get: function () { return this._position(); }},
    loop: {
      get: function () { return this._loop; },
      set: function (value) {
        value = !!value;
        if (value === this._loop) return;
        this._rebase();
        this._loop = value;
        if (this._source) this._source.loop = value;
      }
    },
    playbackRate: {
      get: function () { return this._rate; },
      set: function (value) {
        if (value === this._rate) return;
        this._rebase();
        this._rate = value;
        if (this._source) this._source.playbackRate.value = value;
      }
    },
    volume: {
      get: function () { return this._volume; },
      set: function (value) {
        value = Math.min(1, Math.max(0, value));
        if (value === this._volume) return;
        this._volume = value;
        if (this._gain) engine().agent.setGainValue(this._gain, value);
      }
    }
  });

  return MusicPlayer;
}
