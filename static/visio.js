/* Visioconférence (WebRTC), tableau partagé et discussion pendant l'appel.
   La signalisation passe par le serveur (requêtes périodiques) ; le flux
   audio/vidéo circule directement entre les deux navigateurs. */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var configEl = document.getElementById('visio-config');
    if (!configEl) { return; }
    var config = JSON.parse(configEl.textContent);
    var urls = config.urls;
    var myUserId = config.myUserId;
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

    function postJson(url, body) {
      return fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(body || {})
      }).then(function (res) { return res.json(); });
    }

    function getJson(url) {
      return fetch(url, { credentials: 'same-origin' }).then(function (res) { return res.json(); });
    }

    /* ---------- Discussion ---------- */
    var chatBubbles = document.getElementById('chatBubbles');
    var chatForm = document.getElementById('chatForm');
    var chatInput = document.getElementById('chatInput');
    var lastChatId = config.lastChatId || 0;

    function addChatMessage(body, time, mine) {
      var div = document.createElement('div');
      div.className = 'msg' + (mine ? ' msg-mine' : '');
      var p = document.createElement('p');
      p.textContent = body;
      var meta = document.createElement('span');
      meta.className = 'msg-meta';
      meta.textContent = time;
      div.appendChild(p);
      div.appendChild(meta);
      chatBubbles.appendChild(div);
      chatBubbles.scrollTop = chatBubbles.scrollHeight;
    }

    chatBubbles.scrollTop = chatBubbles.scrollHeight;
    chatForm.addEventListener('submit', function (event) {
      event.preventDefault();
      var body = chatInput.value.trim();
      if (!body) { return; }
      chatInput.value = '';
      postJson(urls.chatSend, { body: body }).then(function (data) {
        if (data && data.message && data.message.id > lastChatId) {
          lastChatId = data.message.id;
          addChatMessage(data.message.body, data.message.created_at, true);
        }
      }).catch(function () { chatInput.value = body; });
    });

    function pollChat() {
      getJson(urls.chatPoll + '?since=' + lastChatId).then(function (data) {
        (data.messages || []).forEach(function (msg) {
          if (msg.id <= lastChatId) { return; }
          lastChatId = msg.id;
          addChatMessage(msg.body, msg.created_at, msg.sender_id === myUserId);
        });
      }).catch(function () {});
    }
    setInterval(pollChat, 2500);

    /* ---------- Tableau partagé ---------- */
    var canvas = document.getElementById('wbCanvas');
    var ctx = canvas.getContext('2d');
    var swatches = document.querySelectorAll('.swatch');
    var widthInput = document.getElementById('wbWidth');
    var eraseBtn = document.getElementById('wbErase');
    var color = '#1f2937';
    var mode = 'draw';
    var drawing = false;
    var points = [];
    var lastStrokeId = 0;
    var history = [];

    function resizeCanvas() {
      var rect = canvas.getBoundingClientRect();
      var ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(rect.width * ratio));
      canvas.height = Math.max(1, Math.round(rect.height * ratio));
      redraw();
    }

    function drawStroke(data) {
      var pts = data.points || [];
      if (!pts.length) { return; }
      var scale = canvas.width / (canvas.getBoundingClientRect().width || 1);
      ctx.save();
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.lineWidth = (data.width || 3) * scale;
      ctx.globalCompositeOperation = data.mode === 'erase' ? 'destination-out' : 'source-over';
      ctx.strokeStyle = data.mode === 'erase' ? '#000' : (data.color || '#1f2937');
      ctx.beginPath();
      ctx.moveTo(pts[0][0] * canvas.width, pts[0][1] * canvas.height);
      if (pts.length === 1) { ctx.lineTo(pts[0][0] * canvas.width + 0.01, pts[0][1] * canvas.height + 0.01); }
      for (var i = 1; i < pts.length; i++) { ctx.lineTo(pts[i][0] * canvas.width, pts[i][1] * canvas.height); }
      ctx.stroke();
      ctx.restore();
    }

    function redraw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      history.forEach(function (entry) { if (entry.kind === 'stroke') { drawStroke(entry.data); } });
    }

    function selectColor(value) {
      color = value;
      mode = 'draw';
      eraseBtn.setAttribute('aria-pressed', 'false');
      swatches.forEach(function (s) { s.setAttribute('aria-pressed', s.getAttribute('data-color') === value ? 'true' : 'false'); });
    }
    swatches.forEach(function (s) {
      s.addEventListener('click', function () { selectColor(s.getAttribute('data-color')); });
    });
    eraseBtn.addEventListener('click', function () {
      mode = 'erase';
      eraseBtn.setAttribute('aria-pressed', 'true');
      swatches.forEach(function (s) { s.setAttribute('aria-pressed', 'false'); });
    });

    function toNormalized(evt) {
      var rect = canvas.getBoundingClientRect();
      var x = (evt.clientX - rect.left) / rect.width;
      var y = (evt.clientY - rect.top) / rect.height;
      return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))];
    }

    canvas.addEventListener('pointerdown', function (evt) {
      drawing = true;
      points = [toNormalized(evt)];
      canvas.setPointerCapture(evt.pointerId);
    });
    canvas.addEventListener('pointermove', function (evt) {
      if (!drawing || points.length >= 2000) { return; }
      points.push(toNormalized(evt));
      drawStroke({ points: points.slice(-2), mode: mode, color: color, width: parseFloat(widthInput.value) });
    });
    function finishStroke() {
      if (!drawing) { return; }
      drawing = false;
      var stroke = { kind: 'stroke', points: points, mode: mode, color: color, width: parseFloat(widthInput.value) };
      points = [];
      if (!stroke.points.length) { return; }
      postJson(urls.wbDraw, stroke).then(function (data) {
        if (data && data.id) {
          history.push({ id: data.id, kind: 'stroke', sender_id: myUserId, data: stroke });
          lastStrokeId = Math.max(lastStrokeId, data.id);
        }
      }).catch(function () {});
    }
    canvas.addEventListener('pointerup', finishStroke);
    canvas.addEventListener('pointercancel', finishStroke);
    canvas.addEventListener('pointerleave', finishStroke);

    document.getElementById('wbClear').addEventListener('click', function () {
      if (!window.confirm('Effacer tout le tableau pour les deux participants ?')) { return; }
      history = [];
      redraw();
      postJson(urls.wbDraw, { kind: 'clear' }).then(function (data) {
        if (data && data.id) { lastStrokeId = Math.max(lastStrokeId, data.id); }
      }).catch(function () {});
    });

    document.getElementById('wbUndo').addEventListener('click', function () {
      postJson(urls.wbUndo, {}).then(function (data) {
        if (data && data.ok) {
          history = history.filter(function (e) { return e.id !== data.target_id; });
          lastStrokeId = Math.max(lastStrokeId, data.id);
          redraw();
        }
      }).catch(function () {});
    });

    function pollWhiteboard() {
      getJson(urls.wbPoll + '?since=' + lastStrokeId).then(function (data) {
        var full = false;
        (data.strokes || []).forEach(function (entry) {
          lastStrokeId = Math.max(lastStrokeId, entry.id);
          if (entry.sender_id === myUserId && entry.kind === 'stroke' &&
              history.some(function (e) { return e.id === entry.id; })) { return; }
          if (entry.kind === 'clear') { history = []; full = true; }
          else if (entry.kind === 'undo') {
            var target = entry.data && entry.data.target_id;
            history = history.filter(function (e) { return e.id !== target; });
            full = true;
          } else if (entry.kind === 'stroke') {
            history.push(entry);
            if (!full) { drawStroke(entry.data); }
          }
        });
        if (full) { redraw(); }
      }).catch(function () {});
    }
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);
    pollWhiteboard();
    setInterval(pollWhiteboard, 1500);

    /* ---------- Appel vidéo ---------- */
    var statusEl = document.getElementById('visioStatus');
    var localVideo = document.getElementById('localVideo');
    var remoteVideo = document.getElementById('remoteVideo');
    var startBtn = document.getElementById('startBtn');
    var leaveBtn = document.getElementById('leaveBtn');
    var muteBtn = document.getElementById('muteBtn');
    var camBtn = document.getElementById('camBtn');
    var pc = null;
    var localStream = null;
    var lastSignalId = 0;
    var inCall = false;
    var busy = false;

    function setStatus(text) { statusEl.textContent = text; }
    function sendSignal(type, payload) { return postJson(urls.send, { type: type, payload: payload }).catch(function () {}); }
    function showCallButtons(active) {
      startBtn.hidden = active;
      leaveBtn.hidden = !active;
      muteBtn.hidden = !active;
      camBtn.hidden = !active;
    }

    function createPeer() {
      var conn = new RTCPeerConnection({ iceServers: (config.stunUrls || []).map(function (u) { return { urls: u }; }) });
      conn.onicecandidate = function (event) { if (event.candidate) { sendSignal('candidate', event.candidate.toJSON()); } };
      conn.ontrack = function (event) {
        remoteVideo.srcObject = event.streams[0];
        setStatus('En communication avec ' + config.partnerName + '.');
      };
      conn.onconnectionstatechange = function () {
        if (inCall && ['disconnected', 'failed'].indexOf(conn.connectionState) !== -1) {
          setStatus('La connexion a été interrompue.');
        }
      };
      return conn;
    }

    function openCamera() {
      if (localStream) { return Promise.resolve(localStream); }
      return navigator.mediaDevices.getUserMedia({ video: true, audio: true }).then(function (stream) {
        localStream = stream;
        localVideo.srcObject = stream;
        return stream;
      });
    }

    function startCall() {
      if (busy) { return; }
      busy = true;
      startBtn.disabled = true;
      setStatus('Ouverture de la caméra…');
      openCamera().then(function () {
        pc = createPeer();
        localStream.getTracks().forEach(function (t) { pc.addTrack(t, localStream); });
        return pc.createOffer();
      }).then(function (offer) {
        return pc.setLocalDescription(offer).then(function () { return sendSignal('offer', { type: offer.type, sdp: offer.sdp }); });
      }).then(function () {
        inCall = true;
        showCallButtons(true);
        setStatus('Appel lancé, en attente de ' + config.partnerName + '…');
      }).catch(function (err) {
        setStatus("Impossible d'accéder à la caméra ou au micro (" + (err && err.name ? err.name : 'erreur') + ').');
      }).then(function () { busy = false; startBtn.disabled = false; });
    }

    function answerCall(offer) {
      setStatus('Ouverture de la caméra…');
      return openCamera().then(function () {
        if (pc) { pc.close(); }
        pc = createPeer();
        localStream.getTracks().forEach(function (t) { pc.addTrack(t, localStream); });
        return pc.setRemoteDescription(offer);
      }).then(function () { return pc.createAnswer(); }).then(function (answer) {
        return pc.setLocalDescription(answer).then(function () { return sendSignal('answer', { type: answer.type, sdp: answer.sdp }); });
      }).then(function () {
        inCall = true;
        showCallButtons(true);
        setStatus('Connexion en cours…');
      }).catch(function (err) {
        setStatus("Impossible d'accéder à la caméra ou au micro (" + (err && err.name ? err.name : 'erreur') + ').');
      });
    }

    function endCall(notify) {
      if (notify && inCall) { sendSignal('leave', {}); }
      if (pc) { pc.close(); pc = null; }
      if (localStream) { localStream.getTracks().forEach(function (t) { t.stop(); }); localStream = null; }
      localVideo.srcObject = null;
      remoteVideo.srcObject = null;
      inCall = false;
      showCallButtons(false);
      setStatus('Appel terminé.');
    }

    var handling = Promise.resolve();
    function pollSignals() {
      getJson(urls.poll + '?since=' + lastSignalId).then(function (data) {
        (data.signals || []).forEach(function (sig) {
          if (sig.id <= lastSignalId) { return; }
          lastSignalId = sig.id;
          handling = handling.then(function () {
            if (sig.type === 'offer' && !inCall) { return answerCall(sig.payload); }
            if (sig.type === 'answer' && pc) { return pc.setRemoteDescription(sig.payload).catch(function () {}); }
            if (sig.type === 'candidate' && pc) { return pc.addIceCandidate(sig.payload).catch(function () {}); }
            if (sig.type === 'leave') { endCall(false); setStatus(config.partnerName + " a quitté l'appel."); }
            return null;
          });
        });
      }).catch(function () {});
    }
    setInterval(pollSignals, 1500);

    startBtn.addEventListener('click', startCall);
    leaveBtn.addEventListener('click', function () { endCall(true); });
    window.addEventListener('pagehide', function () { if (inCall) { sendSignal('leave', {}); } });
    muteBtn.addEventListener('click', function () {
      var track = localStream && localStream.getAudioTracks()[0];
      if (!track) { return; }
      track.enabled = !track.enabled;
      muteBtn.textContent = track.enabled ? 'Couper le micro' : 'Réactiver le micro';
    });
    camBtn.addEventListener('click', function () {
      var track = localStream && localStream.getVideoTracks()[0];
      if (!track) { return; }
      track.enabled = !track.enabled;
      camBtn.textContent = track.enabled ? 'Couper la caméra' : 'Réactiver la caméra';
    });
  });
})();
