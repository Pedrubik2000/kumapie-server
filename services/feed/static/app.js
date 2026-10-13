// German feed: vertical swipe through short clips; the visible one plays, words open their gloss.
"use strict";

const feedEl = document.getElementById("feed");
const tpl = document.getElementById("slide");
const popup = document.getElementById("popup");
const shade = document.getElementById("shade");
const STATUS = { u: "new word", l: "learning", k: "known" };
// Only the visible clip holds a video: Firefox on Android froze the picture (sound kept playing) with
// five loaded at once, most likely out of video decoders. The next clips are fetched into the browser
// cache instead, so they still start at once.

const seen = new Set();   // ids shown this session, sent as `exclude` so nothing repeats
const slides = [];        // {el, video, clip}
let active = -1;
let loading = false;
let started = false;
let rate = 1;
// Auto-scroll: move on after this many plays of a clip (0 = off). Remembered on this device.
const AUTO_STEPS = [0, 1, 2, 3, 5];
let autoPlays = 0;
try { autoPlays = AUTO_STEPS.includes(+localStorage.getItem("feed.auto")) ? +localStorage.getItem("feed.auto") : 0; } catch {}
let popupFor = null;      // {slide, wasPlaying, el}
let audio = null;

async function fetchMore(n = 10) {
  if (loading) return 0;
  loading = true;
  try {
    const r = await fetch("api/feed", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ n, exclude: [...seen] }) });
    const data = await r.json();
    if (data.wrapped) seen.clear();
    for (const clip of data.clips) addSlide(clip);
    return data;
  } catch (e) {
    console.error(e);
    return null;
  } finally {
    loading = false;
  }
}

function addSlide(clip) {
  seen.add(clip.id);
  const el = tpl.content.firstElementChild.cloneNode(true);
  const video = el.querySelector("video");
  // The episode itself: #t= starts it at the clip, tick() stops it at clip.end.
  video.dataset.src = `clips/${clip.id}.mp4?ep=1#t=${clip.start}`;
  el.querySelector(".vtitle").textContent = clip.title;
  el.querySelector(".at").textContent = clip.at;
  const lv = el.querySelector(".level");
  lv.textContent = `i+${clip.level}`;
  lv.classList.toggle("i1", clip.level > 0);

  const lines = el.querySelector(".lines");
  clip.cues.forEach((cue, i) => {
    const p = document.createElement("p");
    p.className = "cue";
    p.dataset.i = i;
    for (const [text, infl] of cue.seg) {
      if (!infl) { p.append(text); continue; }
      const s = document.createElement("span");
      s.className = "w " + (clip.words[infl]?.s || "u");
      s.textContent = text;
      s.dataset.w = infl;
      p.append(s);
    }
    lines.append(p);
  });
  el.querySelector(".en-text").textContent = clip.english || "(no English line)";

  const slide = { el, video, clip, cueEls: [...lines.children] };
  slides.push(slide);
  feedEl.append(el);
  observer.observe(el);

  video.addEventListener("click", () => togglePlay(slide));
  video.addEventListener("play", () => el.classList.remove("paused"));
  video.addEventListener("pause", () => el.classList.add("paused"));
  video.addEventListener("waiting", () => el.classList.add("loading"));
  video.addEventListener("playing", () => el.classList.remove("loading"));
  el.querySelector(".replay").addEventListener("click", () => { slide.plays = 0; video.currentTime = clip.start; play(slide); });
  el.querySelector(".auto").addEventListener("click", () => {
    setAuto(AUTO_STEPS[(AUTO_STEPS.indexOf(autoPlays) + 1) % AUTO_STEPS.length]);
  });
  // No `loop` attribute: each end counts a play, then the clip restarts or the feed moves on.
  slide.onEnd = () => {
    slide.plays = (slide.plays || 0) + 1;
    if (autoPlays && slide.plays >= autoPlays && !slide.hold && slides[active] === slide && slides[active + 1]) {
      slide.userPaused = true;  // stays put while the feed scrolls on
      video.pause();
      feedEl.scrollTo({ top: (active + 1) * feedEl.clientHeight, behavior: "smooth" });
    } else {
      video.currentTime = clip.start;
      play(slide);
    }
  };
  video.addEventListener("ended", slide.onEnd);  // a clip that runs to the episode's end
  // The clip's end (tick() is finer, but requestAnimationFrame can be throttled; this always fires).
  video.addEventListener("timeupdate", () => {
    if (!video.paused && video.currentTime >= clip.end && slides[active] === slide) slide.onEnd();
  });
  el.querySelector(".speed").addEventListener("click", () => setRate(rate === 1 ? 0.75 : 1));
  const en = el.querySelector(".en");
  const enBtn = el.querySelector(".en-btn");
  // Opening the English or the German lines keeps this clip looping (no auto-scroll) until a swipe.
  const toggleEn = () => { slide.hold = true; enBtn.classList.toggle("on", !en.classList.toggle("hidden-en")); };
  en.addEventListener("click", toggleEn);
  enBtn.addEventListener("click", toggleEn);
  // The German lines start blurred (listen first); the first tap shows them, later taps open words.
  lines.classList.add("blurred");
  lines.addEventListener("click", (ev) => {
    if (lines.classList.contains("blurred")) { lines.classList.remove("blurred"); slide.hold = true; return; }
    const w = ev.target.closest(".w");
    if (w) openPopup(slide, w);
  });
  updateSpeedButtons();
  updateAutoButtons();
}

function setAuto(n) {
  autoPlays = n;
  try { localStorage.setItem("feed.auto", String(n)); } catch {}
  updateAutoButtons();
}

function updateAutoButtons() {
  for (const s of slides) {
    const b = s.el.querySelector(".auto");
    b.textContent = autoPlays ? `⏭ ${autoPlays}×` : "⏭ off";
    b.classList.toggle("on", autoPlays > 0);
  }
}

function setRate(r) {
  rate = r;
  for (const s of slides) s.video.playbackRate = r;
  updateSpeedButtons();
}

function updateSpeedButtons() {
  for (const s of slides) {
    const b = s.el.querySelector(".speed");
    b.textContent = rate === 1 ? "1×" : "0.75×";
    b.classList.toggle("on", rate !== 1);
  }
}

function load(slide) {
  const v = slide.video;
  if (!v.getAttribute("src")) {
    v.src = v.dataset.src;
    v.preload = "auto";
    v.playbackRate = rate;
  }
}

function unload(slide) {
  const v = slide.video;
  if (v.getAttribute("src")) {
    v.pause();
    v.removeAttribute("src");
    v.load();  // frees the decoder; the poster shows again
  }
}

function play(slide) {
  load(slide);
  slide.userPaused = false;
  slide.video.playbackRate = rate;
  if (slide.video.readyState < 3) slide.el.classList.add("loading");
  slide.video.play().catch((e) => {
    slide.el.classList.remove("loading");
    slide.el.classList.add("paused");
    // Blocked by the browser: wait for a tap. Anything else (interrupted, suspended): the watchdog retries.
    if (e.name === "NotAllowedError") slide.userPaused = true;
  });
}

function togglePlay(slide) {
  if (slide.video.paused) play(slide);
  else { slide.userPaused = true; slide.video.pause(); }
}

function activate(i) {
  if (i === active) return;
  if (slides[i]) slides[i].plays = 0;
  closePopup(false);
  active = i;
  slides.forEach((s, j) => { if (j !== i) unload(s); });
  const s = slides[i];
  if (started) {
    s.video.currentTime = s.clip.start;
    play(s);
  }
  if (slides.length - i <= 4) fetchMore();
}

const observer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting && e.intersectionRatio >= 0.6) {
      activate(slides.findIndex((s) => s.el === e.target));
    }
  }
}, { root: feedEl, threshold: [0.6] });

// The visible clip from the scroll position too: the observer alone can miss fast swipes.
let scrollTimer = 0;
feedEl.addEventListener("scroll", () => {
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(() => {
    const i = Math.round(feedEl.scrollTop / feedEl.clientHeight);
    if (slides[i]) activate(i);
  }, 120);
}, { passive: true });

// Watchdog: the visible clip plays unless it was paused on purpose (tap, word popup).
setInterval(() => {
  const s = slides[active];
  if (started && s && s.video.paused && !s.video.ended && !s.userPaused && !popupFor && !document.hidden) play(s);
}, 1000);

// The line being spoken, from the cue times (relative to the clip), and the clip's end.
function tick() {
  const s = slides[active];
  if (s && !s.video.paused) {
    if (s.video.currentTime >= s.clip.end) s.onEnd();
    const t = s.video.currentTime - s.clip.start;
    s.clip.cues.forEach((c, i) => s.cueEls[i].classList.toggle("now", t >= c.start && t < c.end + 0.15));
  }
  requestAnimationFrame(tick);
}

function openPopup(slide, wordEl) {
  closePopup(false);
  const words = slide.clip.words;
  const info = words[wordEl.dataset.w] || { s: "u", g: "" };
  const line = wordEl.closest(".cue").dataset.i;
  // The word's definition for this line (definitions v2), if written: same parts as on the TV.
  const def = (slide.clip.defs || {})[`${line}|${wordEl.dataset.w}`];
  popupFor = { slide, wasPlaying: !slide.video.paused, el: wordEl, word: wordEl.textContent,
               defKey: def ? `${slide.clip.id}|${line}|${wordEl.dataset.w}` : null };
  slide.video.pause();
  wordEl.classList.add("sel");
  const g = document.getElementById("pop-gloss");
  const h2 = document.getElementById("pop-head");
  const sub = document.getElementById("pop-sub");
  const defBox = document.getElementById("pop-def");
  document.getElementById("pop-inner").classList.add("hidden");
  document.getElementById("pop-say-def").classList.toggle("hidden", !def);
  if (def) {
    const heading = def.lemma || wordEl.textContent;
    document.getElementById("pop-word").textContent = heading;
    sub.textContent = heading.toLowerCase() === wordEl.textContent.toLowerCase() ? "" : wordEl.textContent;
    h2.classList.add("hidden");
    g.classList.add("hidden");
    document.getElementById("pop-en").textContent = def.en;
    const de = document.getElementById("pop-de");
    de.textContent = "";
    for (const [text, w] of def.de) {
      if (!w) { de.append(text); continue; }
      const s = document.createElement("span");
      s.className = "w " + (words[w]?.s || "u");
      s.textContent = text;
      s.dataset.w = w;
      de.append(s);
    }
    const gr = document.getElementById("pop-gr");
    gr.textContent = def.gr || "";
    gr.classList.toggle("hidden", !def.gr);
    // Every word of the German definition known: read it first, the English under it.
    defBox.classList.toggle("de-first", def.de.every(([, w]) => !w || words[w]?.s === "k"));
    defBox.classList.remove("hidden");
  } else {
    document.getElementById("pop-word").textContent = wordEl.textContent;
    sub.textContent = "";
    defBox.classList.add("hidden");
    g.classList.remove("hidden");
    // "sehe aus = to look (form of aussehen)": the part before " = " as a subheading, the meaning below.
    const [head, meaning] = info.g.includes(" = ") ? info.g.split(/ = (.*)/s) : ["", info.g];
    h2.textContent = head;
    h2.classList.toggle("hidden", !head || head.toLowerCase() === wordEl.textContent.toLowerCase());
    g.textContent = meaning || "No meaning written yet.";
    g.classList.toggle("none", !info.g);
  }
  document.getElementById("pop-status").textContent = STATUS[info.s];
  popup.classList.remove("hidden");
  shade.classList.remove("hidden");
  // Below the word if it fits, else above it.
  const r = wordEl.getBoundingClientRect();
  const h = popup.offsetHeight;
  popup.style.top = (r.bottom + 10 + h < window.innerHeight ? r.bottom + 10 : Math.max(10, r.top - h - 10)) + "px";
  say(wordEl.textContent);
}

function closePopup(resume = true) {
  if (!popupFor) return;
  const { slide, wasPlaying, el } = popupFor;
  popupFor = null;
  el.classList.remove("sel");
  popup.classList.add("hidden");
  shade.classList.add("hidden");
  if (resume && wasPlaying && slides[active] === slide) play(slide);
}

function say(word) { sayUrl(`api/tts?w=${encodeURIComponent(word.toLowerCase())}`); }

function sayUrl(url) {
  if (audio) audio.pause();
  audio = new Audio(url);
  audio.play().catch(() => {});
}

// A word inside the German definition: its own meaning and status under the definition, and its sound.
document.getElementById("pop-de").addEventListener("click", (ev) => {
  const w = ev.target.closest(".w");
  if (!w || !popupFor) return;
  document.querySelectorAll("#pop-de .w.sel").forEach((x) => x.classList.remove("sel"));
  w.classList.add("sel");
  const info = popupFor.slide.clip.words[w.dataset.w] || { s: "u", g: "" };
  const meaning = info.g.includes(" = ") ? info.g.split(/ = (.*)/s)[1] : info.g;
  const inner = document.getElementById("pop-inner");
  inner.textContent = `${w.textContent}: ${meaning || "no meaning yet"} · ${STATUS[info.s]}`;
  inner.classList.remove("hidden");
  say(w.textContent);
});
document.getElementById("pop-say-def").addEventListener("click", () => {
  if (popupFor?.defKey) sayUrl(`api/tts?d=${encodeURIComponent(popupFor.defKey)}`);
});

shade.addEventListener("click", () => closePopup(true));
document.getElementById("pop-say").addEventListener("click", () => { if (popupFor) say(popupFor.word); });
feedEl.addEventListener("scroll", () => closePopup(false), { passive: true });

async function init() {
  const info = document.getElementById("start-info");
  const btn = document.getElementById("start-btn");
  const data = await fetchMore(10);
  if (!data) { info.textContent = "Could not reach the feed server."; return; }
  const pool = data.pool.i0 + data.pool.i1;
  if (!data.clips.length) {
    info.textContent = "No i+0 or i+1 clips yet. Build the feed, and open the German Anki so known words load.";
    return;
  }
  info.textContent = `${pool} clips for you: ${data.pool.i0} i+0, ${data.pool.i1} i+1`;
  btn.disabled = false;
  btn.addEventListener("click", () => {
    started = true;
    document.getElementById("start").classList.add("gone");
    const s = slides[Math.max(active, 0)];
    if (s) { s.video.currentTime = s.clip.start; play(s); }
  });
}

requestAnimationFrame(tick);
init();
