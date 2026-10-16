// kumapie on the PC (code\kumapie_pc_plan.md): Home, Show and a scene-by-scene player over /api/tv, like the TV app.
// The scene logic is :core's SceneController.kt; rating and mining arrive with the kuma3 bridge (step 2).
"use strict";

const app = document.getElementById("app");
const $ = (tag, props = {}, ...kids) => { const el = Object.assign(document.createElement(tag), props); el.append(...kids); return el; };
const STATE = { k: "known", u: "new word", d: "due today", b: "mined, not reviewed yet", n: "learning" };
const DECKS = { de: "Deutsch Mined", en: "English Mined", ja: "Japanese Mined" };
const LANGS = { de: "Deutsch", en: "English", ja: "日本語" };
const MODES = ["hidden", "blurred", "target", "both", "english"];

function pref(key, dflt) { try { const v = localStorage.getItem("pc." + key); return v === null ? dflt : JSON.parse(v); } catch { return dflt; } }
function setPref(key, v) { try { localStorage.setItem("pc." + key, JSON.stringify(v)); } catch {} }
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
  .then(r => r.ok ? r.json() : Promise.reject(r.status));

let person = new URLSearchParams(location.search).get("person");
let shows = null;       // /api/tv/shows for the person, visual novels left out (not on the PC)
let player = null;      // the open player, closed on leaving it
const anki = { up: false, synced: 0 };  // kuma3 desktop through dojo-feed's /api/anki (AnkiConnect)
const fetchOk = url => fetch(url).then(r => r.ok ? r : Promise.reject(r.status));

/** Is kuma3 open? Asked on load and whenever the page gets focus (no timer). Repaints the player when it changes. */
async function checkAnki() {
  const was = anki.up;
  try { Object.assign(anki, await (await fetchOk("api/anki/status")).json()); } catch { anki.up = false; }
  const pill = document.getElementById("anki");
  const at = anki.synced ? ` · synced ${new Date(anki.synced * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  pill.textContent = anki.up ? `kuma3 ● connected${at}` : "kuma3 closed: colours from your phone, no mining";
  pill.className = `pill ${anki.up ? "on" : "off"}`;
  pill.title = anki.up ? "Don't review on the phone during a PC sitting: both sync through AnkiWeb." : "Open kuma3 desktop to rate and mine.";
  if (was !== anki.up && player) player.reloadCards();
}
async function syncAnki() {
  if (!anki.up) return;
  try { Object.assign(anki, await post("api/anki/sync", {})); } catch (e) { console.error(e); }
  checkAnki();
}
addEventListener("focus", checkAnki);

async function loadShows() {
  if (!person) person = (await (await fetch("api/tv/people")).json())[0].id;  // the config's first sync person: Pedro
  const data = await (await fetch(`api/tv/shows?person=${person}`)).json();
  shows = data.shows.filter(s => s.kind !== "visual_novels");
}

function langTabs(current) {
  const nav = document.getElementById("langs");
  nav.replaceChildren(...[...new Set(shows.map(s => s.lang))].map(l => $("button", {
    textContent: LANGS[l] || l, className: l === current ? "on" : "",
    onclick: () => { setPref("lang", l); location.hash = "#/"; route(); },
  })));
}

// ------------------------------------------------------------ Home and Show

function poster(sh) {
  return $("a", { className: "card", href: `#/show/${sh.id}` },
    $("img", { src: sh.poster, alt: "", loading: "lazy" }),
    $("div", { className: "t" }, sh.title, $("div", { className: "s", textContent: easyText(sh) })));
}
function easyText(sh) {
  const easy = sh.episodes.reduce((n, e) => n + e.easy, 0);
  return `${sh.episodes.length} ep · ${easy} easy scenes`;
}
function epCard(sh, e) {
  return $("a", { className: "card wide", href: `#/ep/${e.id}` },
    $("img", { src: e.thumb, alt: "", loading: "lazy" }),
    $("div", { className: "bar" }, $("i", { style: `width:${100 * e.seen / (e.scenes || 1)}%` })),
    $("div", { className: "t" }, sh.title, $("div", { className: "s", textContent: e.title })));
}

function home() {
  const langs = [...new Set(shows.map(s => s.lang))];
  const lang = langs.includes(pref("lang")) ? pref("lang") : langs[0];
  langTabs(lang);
  const mine = shows.filter(s => s.lang === lang);
  const going = mine.flatMap(sh => sh.episodes.filter(e => e.resumed_at).map(e => [sh, e]))
    .sort((a, b) => b[1].resumed_at - a[1].resumed_at).slice(0, 8);
  const out = [];
  if (going.length) out.push($("h2", { textContent: "Continue watching" }), $("div", { className: "row" }, ...going.map(([sh, e]) => epCard(sh, e))));
  for (const kind of [...new Set(mine.map(s => s.kind))]) {
    out.push($("h2", { textContent: kind[0].toUpperCase() + kind.slice(1) }),
      $("div", { className: "row" }, ...mine.filter(s => s.kind === kind).map(poster)));
  }
  app.replaceChildren(...out);
}

function show(id) {
  const sh = shows.find(s => s.id === id);
  if (!sh) return app.replaceChildren("No such show.");
  langTabs(sh.lang);
  const last = sh.episodes.reduce((a, e) => (e.resumed_at > (a?.resumed_at || 0) ? e : a), null);
  app.replaceChildren($("div", { className: "show" },
    $("img", { src: sh.poster, alt: "" }),
    $("div", {},
      $("h2", { textContent: sh.title + (sh.year ? ` (${sh.year})` : "") }),
      $("p", { textContent: sh.description || "" }),
      $("div", { className: "eps" }, ...sh.episodes.map(e => $("a", { href: `#/ep/${e.id}`, className: e === last ? "last" : "" },
        $("img", { src: e.thumb, alt: "", loading: "lazy" }),
        // the episode's own title only once it is watched (spoilers)
        $("div", {}, e.title + (e.seen >= e.scenes && e.episode_title ? ` · ${e.episode_title}` : ""),
          $("div", { className: "bar" }, $("i", { style: `width:${100 * e.seen / (e.scenes || 1)}%` }))),
        $("span", { className: "lv", textContent: `${e.easy} easy` })))))));
}

// ------------------------------------------------------------ Player

class Player {
  constructor(ep) {
    this.ep = ep;
    this.scenes = ep.scenes;
    this.words = ep.words;
    this.opts = { slow: pref("slow", false), newStraight: pref("newStraight", false),
                  playThroughKnown: pref("playThroughKnown", false), pauseAtSceneEnd: pref("pauseAtSceneEnd", true) };
    this.mode = pref("subs", "target");
    this.index = this.startIndex();
    this.stopAt = null; this.stopIsSceneEnd = false; this.atSceneEnd = false;
    this.seenNew = new Set(); this.seenHere = new Set();
    this.watched = 0; this.lastTick = performance.now(); this.lastReport = performance.now(); this.reportedPos = ep.resume;
    this.looked = 0; this.picked = null;
    this.build();
    this.timer = setInterval(() => this.tick(), 100);
    this.onKey = e => this.key(e);
    document.addEventListener("keydown", this.onKey);
    this.onLeave = () => this.report(true);
    addEventListener("pagehide", this.onLeave);
  }

  startIndex() {
    const r = this.ep.resume;
    if (r == null) return 0;
    let i = -1;
    this.scenes.forEach((s, j) => { if (s.start <= r + 0.05) i = j; });
    if (i < 0) return 0;
    return r >= this.scenes[i].end - 0.2 && i + 1 < this.scenes.length ? i + 1 : i;
  }
  get scene() { return this.scenes[this.index]; }
  get pos() { return this.video.currentTime; }
  get playing() { return !this.video.paused; }
  level(sc) { return sc.german ? new Set(sc.cues.flatMap(c => c.seg.filter(([, w]) => w && this.state(w) === "u").map(([, w]) => w))).size : null; }
  /** The colour (KnownWords.shown): marked known white; kuma3's own card (d green, b blue, k white, learning red);
   *  no card of its own (or kuma3 closed): the known words the phone sent. */
  state(w) {
    const x = this.words[w];
    if (!x || x.m) return "k";
    const c = this.cards?.forms[w];
    return !c ? x.s : c === "n" ? "u" : c;
  }
  known(sc) { return this.opts.playThroughKnown && this.level(sc) === 0; }
  pausesAtEnd(sc) { return this.opts.pauseAtSceneEnd && !this.known(sc); }

  build() {
    const opt = (key, label) => $("label", {}, $("input", { type: "checkbox", checked: this.opts[key], onchange: e => {
      this.opts[key] = e.target.checked; setPref(key, e.target.checked);
      if (key === "slow") this.video.playbackRate = e.target.checked ? 0.75 : 1;
      if (this.playing) this.stopAtSceneEnd();
    } }), " " + label);
    this.video = $("video", { src: this.ep.video, preload: "auto", onclick: () => this.togglePlay() });
    this.video.playbackRate = this.opts.slow ? 0.75 : 1;
    this.sceneLabel = $("span");
    this.lv = $("span", { className: "lv" });
    this.modeBtn = $("button", { onclick: () => this.cycleMode(), title: "S" });
    this.subs = $("div", { className: "subs" });
    this.card = $("div", { id: "card" });
    this.trans = $("div", { id: "trans" });
    app.replaceChildren($("div", { className: "player" },
      $("div", { className: "left" },
        $("div", { className: "pbar" },
          $("a", { href: `#/show/${shows.find(s => s.episodes.some(e => e.id === this.ep.id))?.id || ""}`, textContent: "←" }),
          $("span", { className: "title", textContent: `${this.ep.show} · ${this.ep.title}` }),
          this.sceneLabel, this.lv, this.modeBtn,
          $("details", { className: "opts" }, $("summary", { textContent: "Options" }), $("div", {},
            opt("slow", "Speed 0.75×"), opt("newStraight", "Skip nothing"),
            opt("playThroughKnown", "Don't stop on i+0"), opt("pauseAtSceneEnd", "Pause at scene end"))),
          $("button", { textContent: "?", onclick: () => document.getElementById("help").showModal() })),
        this.video, this.subs),
      $("div", { className: "right" }, this.card, this.trans)));
    this.emptyCard();
    this.video.addEventListener("loadedmetadata", () => { this.video.currentTime = this.scene?.start || 0; this.draw(true); }, { once: true });
    this.video.addEventListener("play", () => this.draw());
    this.video.addEventListener("pause", () => this.draw());
    this.drawTranscript();
  }

  // ---------------------------------------------------- actions (SceneController's)

  playScene(i) {
    if (!this.scenes.length) return;
    this.index = Math.max(0, Math.min(i, this.scenes.length - 1));
    this.atSceneEnd = false;
    this.video.currentTime = this.scene.start;
    this.stopAtSceneEnd();
    this.video.play();
    this.draw(true);
  }
  nextScene() { if (this.index < this.scenes.length - 1) this.playScene(this.index + 1); else this.done(); }
  previousScene() { if (this.index > 0) this.playScene(this.index - 1); else { this.playScene(0); this.video.currentTime = 0; } }
  replayLine() {
    const cue = this.currentCue();
    if (!cue) return this.playScene(this.index);
    this.playCue(cue);
  }
  playCue(cue) {
    this.atSceneEnd = false;
    this.video.currentTime = Math.max(cue.start - 0.15, this.scene.start);
    this.stopAt = Math.min(cue.end + 0.25, this.scene.end);
    this.stopIsSceneEnd = this.stopAt === this.scene.end;
    this.video.play();
  }
  togglePlay() {
    if (this.playing) return this.video.pause();
    if (this.atSceneEnd && this.opts.newStraight && this.index < this.scenes.length - 1) {
      this.atSceneEnd = false; this.stopAt = null; return this.video.play();
    }
    if (this.atSceneEnd) return this.nextScene();
    this.stopAtSceneEnd();
    this.video.play();
  }
  cycleMode() {
    this.mode = MODES[(MODES.indexOf(this.mode) + 1) % MODES.length];
    setPref("subs", this.mode);
    this.draw(true);
  }
  stopAtSceneEnd() {
    if (this.scene && this.pausesAtEnd(this.scene)) { this.stopAt = this.scene.end; this.stopIsSceneEnd = true; }
    else this.stopAt = null;
  }
  currentCue() {
    const cues = this.scene?.cues || [];
    let c = null;
    for (const x of cues) if (x.start <= this.pos + 0.05) c = x;
    return c || cues[0] || null;
  }

  tick() {
    const now = performance.now();
    if (this.playing) this.watched += (now - this.lastTick) / 1000;
    this.lastTick = now;
    if (!this.scenes.length) return;
    const p = this.pos;
    if (this.playing && p >= this.scene.end) this.seen(this.scene);
    if (this.stopAt != null && this.playing && p >= this.stopAt) {
      this.video.pause();
      this.atSceneEnd = this.stopIsSceneEnd;
      this.stopAt = null;
      if (this.atSceneEnd) { this.seen(this.scene); if (this.index === this.scenes.length - 1) this.done(); }
    }
    if (!this.atSceneEnd && (!this.pausesAtEnd(this.scene) || this.opts.newStraight)) {  // playing on: follow the video
      let i = -1;
      this.scenes.forEach((s, j) => { if (s.start <= p) i = j; });
      if (i > this.index && (p >= this.scene.end || !this.pausesAtEnd(this.scene))) { this.index = i; this.stopAtSceneEnd(); }
    }
    if (now - this.lastReport > 20000) this.report();
    this.draw();
  }
  seen(sc) {
    if (this.seenHere.has(sc.id)) return;
    this.seenNew.add(sc.id); this.seenHere.add(sc.id);
    this.drawTranscript();
  }

  /** Where the episode is, scenes seen and time watched (as device "pc": the TV's tables, merged by the phones). */
  report(leaving = false) {
    this.lastReport = performance.now();
    const body = { episode: this.ep.id, pos: this.pos, seen: [...this.seenNew], watched: this.watched };
    if (!body.seen.length && this.watched < 1 && this.reportedPos === body.pos) return;
    this.reportedPos = body.pos; this.seenNew.clear(); this.watched = 0;
    if (leaving) navigator.sendBeacon("api/tv/progress", new Blob([JSON.stringify(body)], { type: "application/json" }));
    else post("api/tv/progress", body).catch(console.error);
  }

  close() {
    this.report(true);
    if (anki.up) navigator.sendBeacon("api/anki/sync");  // after a sitting: kuma3 -> AnkiWeb -> the phone
    clearInterval(this.timer);
    document.removeEventListener("keydown", this.onKey);
    removeEventListener("pagehide", this.onLeave);
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
  }

  // ---------------------------------------------------- drawing

  line(cue, sc) {
    const p = $("p", { className: "tg" });
    for (const [text, w] of cue.seg) {
      if (!w) { p.append(text); continue; }
      const x = this.words[w] || {};
      p.append($("button", { className: `w ${this.state(w)}${this.picked?.w === w ? " sel" : ""}`, textContent: text,
        title: sc.g?.[w] || x.g || "", onclick: e => { e.stopPropagation(); this.pick(w, cue, sc); } }));
    }
    return p;
  }

  draw(force = false) {
    const sc = this.scene;
    if (!sc) return;
    const cue = this.playing ? this.currentCue() : null;
    const key = `${this.index}|${this.playing}|${cue?.start}|${this.mode}|${this.picked?.w}`;
    if (!force && key === this.drawn) return;
    this.drawn = key;
    this.sceneLabel.textContent = `Scene ${this.index + 1}/${this.scenes.length}`;
    const lv = this.level(sc);
    this.lv.textContent = lv == null ? "–" : `i+${lv}`;
    this.lv.className = `lv${lv === 0 ? " i0" : lv === 1 ? " i1" : ""}`;
    this.modeBtn.textContent = `Subtitles: ${this.mode}`;
    // Playing: the line being said; paused: all the scene's lines (blurred ones readable, as the phone).
    const cues = this.playing ? (cue && this.pos >= cue.start - 0.05 && this.pos <= cue.end + 0.3 ? [cue] : []) : sc.cues;
    const target = ["blurred", "target", "both"].includes(this.mode), english = ["both", "english"].includes(this.mode);
    this.subs.className = `subs${this.mode === "blurred" ? " blur" : ""}`;
    const out = [];
    for (const c of cues) {
      if (target) { const p = this.line(c, sc); if (!this.playing) p.classList.add("read"); out.push(p); }
      if (english) {
        const en = sc.english.filter(e => e[1] > c.start && e[0] < c.end).map(e => e[2]).join(" ");
        if (en) out.push($("p", { className: "en", textContent: en }));
      }
    }
    if (!this.playing && english && !target && !sc.cues.length) out.push(...sc.english.map(e => $("p", { className: "en", textContent: e[2] })));
    this.subs.replaceChildren(...out);
    this.followTranscript();
  }

  /** The whole episode by scene; scenes not watched yet show their number and level only (Pedro, 2026-10-15). */
  drawTranscript() {
    this.trans.replaceChildren(...this.scenes.map((sc, i) => {
      const seen = sc.seen || this.seenHere.has(sc.id);
      const lv = this.level(sc);
      const el = $("div", { className: `sc${seen ? " seen" : ""}` },
        $("header", { textContent: `Scene ${i + 1} · ${lv == null ? "no words" : "i+" + lv}`, onclick: () => this.playScene(i), style: "cursor:pointer" }));
      if (seen || i === this.index) {
        for (const c of sc.cues) {
          const p = this.line(c, sc);
          p.onclick = () => { this.index = i; this.playCue(c); };
          p.dataset.start = c.start;
          el.append(p);
        }
      }
      return el;
    }));
    this.drawnFor = this.index;
    this.transScene = -1;
    this.followTranscript();
  }
  followTranscript() {
    if (this.drawnFor !== this.index) return this.drawTranscript();  // a new scene: show its text
    if (this.transScene !== this.index) {
      [...this.trans.children].forEach((el, i) => el.classList.toggle("cur", i === this.index));
      this.trans.children[this.index]?.scrollIntoView({ block: "nearest" });
      this.transScene = this.index;
    }
    const cue = this.currentCue();
    for (const p of this.trans.querySelectorAll("p.on")) p.classList.remove("on");
    const on = cue && this.trans.children[this.index]?.querySelector(`p[data-start="${cue.start}"]`);
    if (on) { on.classList.add("on"); }
  }

  // ---------------------------------------------------- the word card

  emptyCard() { this.picked = null; this.card.replaceChildren($("p", { className: "empty", textContent: "Click a coloured word" })); }

  /** A word clicked: pause, fill the card, then every sense from the dictionaries (the line's own sense first). */
  async pick(w, cue, sc) {
    this.video.pause();
    const x = this.words[w] || {};
    this.picked = { w, cue, sc, senses: [sc.g?.[w] || x.g].filter(Boolean), status: null, mining: false };
    this.renderCard();
    this.draw(true);
    this.looked++;
    try {
      const r = await post("api/tv/lookup", { word: w, scene: sc.id });
      x.n = r.n;
      if (this.picked?.w !== w) return;
      this.picked.senses = [...new Set([sc.g?.[w], x.g, ...(r.senses || [])].filter(Boolean))];
      if (!this.picked.mining) this.renderCard();
    } catch (e) { console.error(e); }
  }

  /** The card for [this.picked]: word, state, senses, the line, actions, then the mine form or the last answer. */
  renderCard() {
    const { w, cue, sc, senses, status } = this.picked;
    const x = this.words[w] || {};
    const quote = $("blockquote");
    for (const [text, ww] of cue.seg) quote.append(ww === w ? $("b", { textContent: text }) : text);
    const own = this.cards?.forms[w];
    const why = !anki.up ? "Open kuma3 to rate and mine" : !own ? "No card yet: mine it to rate it" : "";
    const rate = ["Again", "Hard", "Good", "Easy"].map((l, i) => $("button", { textContent: `${i + 1} ${l}`,
      disabled: !anki.up || !own, title: why, onclick: () => this.rate(i + 1) }));
    this.card.replaceChildren(
      $("h3", { textContent: x.lemma || w }, x.r ? $("small", { textContent: `  ${x.r}` }) : ""),
      $("div", { className: "why", textContent: STATE[this.state(w)] + (x.n ? ` · looked up ${x.n}×` : "") }),
      $("ol", {}, ...(senses.length ? senses : ["No meaning in the dictionaries"]).map(s => $("li", { textContent: s }))), quote,
      $("div", { className: "acts" }, ...rate,
        $("button", { textContent: "M Mine", disabled: !anki.up, title: anki.up ? "" : why, onclick: () => this.mineForm() }),
        $("button", { textContent: x.m ? "K Not known" : "K Known", onclick: () => this.markKnown(w) }),
        $("button", { textContent: "A 🔊", onclick: () => this.hear(w) })),
      why ? $("div", { className: "why", textContent: why }) : "",
      status || "");
  }
  /** A line in the card under the actions ("Answered: Good", "Added to Deutsch Mined"), with an optional button. */
  say(text, button) {
    if (!this.picked) return;
    this.picked.status = $("div", { className: "status" }, text, button ? " " : "", button || "");
    this.picked.mining = false;
    this.renderCard();
  }

  async reloadCards() {
    try { this.cards = anki.up ? await (await fetchOk(`api/anki/cards?lang=${this.ep.lang}`)).json() : null; }
    catch (e) { this.cards = null; await checkAnki(); }
    this.drawTranscript();
    this.draw(true);
  }

  /** 1-4 on the card: every due card of the word, as the phone (one rating answers them all). */
  async rate(ease) {
    const w = this.picked?.w;
    if (!w || !anki.up || !this.cards?.forms[w]) return;
    const label = ["Again", "Hard", "Good", "Easy"][ease - 1];
    try {
      const r = await post("api/anki/rate", { lang: this.ep.lang, word: w, ease });
      await this.reloadCards();
      this.say(`Answered: ${label}` + (r.cards > 1 ? ` (${r.answered} cards)` : ""),
        $("button", { textContent: "Undo", onclick: () => this.undoRating(r.answered) }));
    } catch (e) { this.say(`Couldn't rate: ${e}`); }
  }
  async undoRating(n) {
    try {
      for (let i = 0; i < n; i++) await post("api/anki/undo", {});
      await this.reloadCards();
      this.say("Rating undone");
    } catch (e) { this.say(`Couldn't undo: ${e}. Ctrl+Z in kuma3`); }
  }

  /** M: the mine form in the card (no sheet). Enter adds, Esc closes. */
  mineForm() {
    const p = this.picked;
    if (!p || !anki.up) return;
    const deck = DECKS[this.ep.lang];
    const note = this.cards?.notes[p.w];
    if (note) return this.say(`Already in ${deck}`, $("button", { textContent: "Open in Anki", onclick: () => post("api/anki/browse", { note }) }));
    p.mining = true;
    const type = pref("mineType", "word");
    const radio = (v, label) => $("label", {}, $("input", { type: "radio", name: "kind", value: v, checked: type === v }), ` ${label} `);
    const sense = $("select", { name: "gloss" }, ...p.senses.map(s => $("option", { value: s, textContent: s })));
    const en = p.sc.english.filter(e => e[1] > p.cue.start && e[0] < p.cue.end).map(e => e[2]).join(" ");
    const english = $("textarea", { name: "english", rows: 2, value: en });
    const form = $("form", { className: "mine" },
      $("div", {}, radio("word", "Word card"), radio("sentence", "Sentence card")),
      p.senses.length ? sense : "", english,
      $("div", { className: "acts" }, $("button", { type: "submit", textContent: `Add to ${deck}` }),
        $("button", { type: "button", textContent: "Cancel", onclick: () => { p.mining = false; this.renderCard(); } })));
    form.onsubmit = e => { e.preventDefault(); this.mine(form.kind.value, sense.value, english.value); };
    english.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } };
    form.onkeydown = e => { if (e.key === "Escape") { p.mining = false; this.renderCard(); } };
    this.card.append(form);
    form.querySelector("input:checked").focus();
  }
  async mine(kind, gloss, english) {
    const p = this.picked;
    setPref("mineType", kind);
    this.say(this.ep.lang === "ja" ? "Cutting the line's audio…" : "Cutting the clip…");
    try {
      const r = await post("api/anki/mine", { episode: this.ep.id, scene: p.sc.id, line: p.sc.cues.indexOf(p.cue),
        word: kind === "word" ? p.w : null, surface: p.cue.seg.find(([, w]) => w === p.w)?.[0], gloss, english });
      await this.reloadCards();
      const undo = $("button", { textContent: "Undo", onclick: async () => {
        await post("api/anki/delete", { note: r.note }).catch(console.error);
        await this.reloadCards();
        this.say("Card removed");
      } });
      this.say(`Added to ${r.deck}`, undo);
      setTimeout(() => undo.remove(), 10000);
    } catch (e) { this.say(`Couldn't mine: ${e}`); }
  }

  hear(w) { new Audio(`api/tts?w=${encodeURIComponent(w)}&lang=${this.ep.lang}`).play().catch(() => {}); }
  async markKnown(w) {
    const x = this.words[w] || (this.words[w] = { s: "u" });
    try { await post("api/tv/known", { word: w, known: !x.m }); x.m = !x.m; } catch (e) { return console.error(e); }
    this.drawTranscript();
    if (this.picked?.w === w) this.renderCard();
  }

  done() {
    const sh = shows.find(s => s.episodes.some(e => e.id === this.ep.id));
    const eps = sh?.episodes || [];
    const next = eps[eps.findIndex(e => e.id === this.ep.id) + 1];
    this.report();
    this.picked = null;
    this.card.replaceChildren($("h3", { textContent: "Episode done" }),
      $("p", { textContent: `${this.seenHere.size} scenes watched · ${this.looked} words looked up` }),
      next ? $("a", { href: `#/ep/${next.id}` }, $("button", { textContent: `Next: ${next.title}` })) : "");
  }

  // ---------------------------------------------------- keys

  key(e) {
    if (e.target.closest("input, textarea, select, form, dialog") || e.ctrlKey || e.altKey || e.metaKey) return;
    const onWord = e.target.classList?.contains("w");
    const k = e.key;
    const act = {
      " ": () => this.togglePlay(),
      Enter: () => onWord ? e.target.click() : this.nextScene(),
      ArrowRight: () => this.nextScene(),
      ArrowLeft: () => e.shiftKey ? this.playScene(this.index) : this.previousScene(),
      r: () => this.replayLine(), s: () => this.cycleMode(),
      k: () => this.picked && this.markKnown(this.picked.w), m: () => this.picked && this.mineForm(),
      1: () => this.rate(1), 2: () => this.rate(2), 3: () => this.rate(3), 4: () => this.rate(4), a: () => this.picked && this.hear(this.picked.w),
      Escape: () => this.emptyCard(),
      "?": () => document.getElementById("help").showModal(),
    }[k.length === 1 ? k.toLowerCase() : k];
    if (!act) return;
    e.preventDefault();
    act();
  }
}

async function openEpisode(id) {
  const r = await fetch(`api/tv/episode/${id}`);
  if (!r.ok) return app.replaceChildren("No such episode.");
  const ep = await r.json();
  langTabs(ep.lang);
  player = new Player(ep);
  player.reloadCards();
}

// ------------------------------------------------------------ routes: #/, #/show/<id>, #/ep/<id>

async function route() {
  if (player) { player.close(); player = null; }
  const [, kind, id] = location.hash.split("/");
  if (kind !== "ep" || !shows) await loadShows();  // fresh positions and levels on every screen but the player
  if (kind === "show") return show(id);
  if (kind === "ep") return openEpisode(id);
  home();
}
addEventListener("hashchange", route);
checkAnki().then(syncAnki);  // the phone's latest reviews before the sitting
route();
