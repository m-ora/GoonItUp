(() => {
  const STORE = "goonitup.v1";
  const SPEED_DEFAULT = 8;
  const PAGE_SIZE = 15;

  const $ = (id) => document.getElementById(id);
  const feed = $("feed");
  const statusEl = $("status");
  const counterEl = $("counter");
  const titleEl = $("title");
  const bylineEl = $("byline");
  const emptyEl = $("empty");
  const playBtn = $("play");
  const muteBtn = $("mute");
  const speedInput = $("speed");
  const apiState = $("api-state");

  const state = {
    items: [],
    after: null,
    exhausted: false,
    loading: false,
    index: 0,
    playing: true,
    muted: true,
    speed: SPEED_DEFAULT,
    shuffle: false,
    hideTimer: 0,
    slideTimer: 0,
    scrollRaf: 0,
    userScrollUntil: 0,
    mediaObserver: null,
    seen: new Set(),
    emptyPages: 0,
    fillPasses: 0,
    source: "rss",
    subs: ["pics"],
    sources: [],
    cols: "auto",
    streams: new Map(),
    streamPos: new Map(),
    colDirs: [],
    query: { sub: "pics", sort: "hot", t: "day" },
  };

  function loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(STORE) || "{}");
    } catch {
      return {};
    }
  }

  function savePrefs() {
    localStorage.setItem(
      STORE,
      JSON.stringify({
        entered: true,
        subs: $("subs").value,
        sort: $("sort").value,
        t: $("time").value,
        cols: $("cols").value,
        speed: state.speed,
        muted: state.muted,
        shuffle: state.shuffle,
        playing: state.playing,
      })
    );
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  const SOURCE_RE = /^(e621|e6|gelbooru|gel|gb|rule34|r34|r34xxx|rule34xxx|realbooru|rb|real|reddit|r)\s*:\s*(.+)$/i;
  const PRESET_KEY = "goonitup.presets.v1";

  function parseSources(raw) {
    return raw
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => {
        const match = part.match(SOURCE_RE);
        if (!match) {
          const name = part.replace(/^r\//i, "");
          return { kind: "reddit", query: name, label: name };
        }
        const prefix = match[1].toLowerCase();
        const query = match[2].trim();
        if (prefix === "e621" || prefix === "e6") return { kind: "e621", query, label: `e621:${query}` };
        if (prefix === "gelbooru" || prefix === "gel" || prefix === "gb") {
          return { kind: "gelbooru", query, label: `gelbooru:${query}` };
        }
        if (prefix === "rule34" || prefix === "r34" || prefix === "r34xxx" || prefix === "rule34xxx") {
          return { kind: "rule34", query, label: `r34:${query}` };
        }
        if (prefix === "realbooru" || prefix === "rb" || prefix === "real") {
          return { kind: "realbooru", query, label: `realbooru:${query}` };
        }
        const name = query.replace(/^r\//i, "");
        return { kind: "reddit", query: name, label: name };
      });
  }

  function parseSubs(raw) {
    return parseSources(raw).map((src) => src.label);
  }

  function loadPresetList() {
    try {
      return JSON.parse(localStorage.getItem(PRESET_KEY) || "[]");
    } catch {
      return [];
    }
  }

  function savePresetList(list) {
    localStorage.setItem(PRESET_KEY, JSON.stringify(list));
  }

  function renderPresets() {
    const select = $("presets");
    const current = select.value;
    select.innerHTML = `<option value="">presets</option>`;
    loadPresetList().forEach((preset) => {
      const option = document.createElement("option");
      option.value = preset.id;
      option.textContent = preset.name;
      select.appendChild(option);
    });
    if ([...select.options].some((opt) => opt.value === current)) select.value = current;
  }

  function isSingle() {
    return state.cols === "1";
  }

  function columnCount() {
    if (isSingle()) return 1;
    if (state.cols !== "auto") return Number(state.cols) || 3;
    return Math.max(2, Math.min(6, Math.round((window.innerWidth || 1200) / 360) || 3));
  }

  function colEls() {
    return [...feed.querySelectorAll(":scope > .col")];
  }

  function pauseAutoScroll() {
    state.userScrollUntil = Date.now() + 1500;
  }

  function onColScroll(event) {
    const col = event.currentTarget;
    if (col.scrollTop + col.clientHeight > col.scrollHeight - 280) loadPage();
    if (!state.hydraf) {
      state.hydraf = true;
      requestAnimationFrame(() => {
        state.hydraf = false;
        hydrateAround();
      });
    }
  }

  function makeCol(i) {
    const col = document.createElement("div");
    col.className = "col";
    col.dataset.col = String(i);
    col.addEventListener("scroll", onColScroll);
    col.addEventListener("wheel", pauseAutoScroll, { passive: true });
    return col;
  }

  function buildColumns() {
    const n = columnCount();
    feed.classList.toggle("single", isSingle());
    feed.classList.toggle("columns", !isSingle());
    feed.innerHTML = "";
    for (let i = 0; i < n; i += 1) feed.appendChild(makeCol(i));
    state.colDirs = Array.from({ length: n }, (_, i) => ({
      dir: 1,
      speed: 0.75 + (i % 4) * 0.12,
      acc: 0,
    }));
  }

  function applyColumns() {
    if (state.mediaObserver) {
      state.mediaObserver.disconnect();
      state.mediaObserver = null;
    }
    buildColumns();
    if (state.items.length) renderItems(0);
    feed.querySelectorAll("video").forEach((video) => {
      video.loop = !isSingle();
    });
    if (state.playing && !isSingle()) startColScroll();
    else stopColScroll();
  }

  async function refreshApiStatus() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      state.source = data.source || "rss";
      if (data.connected) {
        apiState.textContent = data.username ? `Connected as u/${data.username}` : "Connected to Reddit API";
      } else {
        apiState.textContent = "Using Reddit RSS (no login)";
      }
      return data;
    } catch {
      apiState.textContent = "Local proxy is not running. Use start.bat.";
      return null;
    }
  }

  function slideEl(index) {
    return feed.querySelector(`[data-index="${index}"]`);
  }

  function updateMeta() {
    const item = state.items[state.index];
    const total = state.items.length;
    counterEl.textContent = total ? `${state.index + 1} / ${total}${state.exhausted ? "" : "+"}` : "0 / 0";
    if (!item) {
      titleEl.textContent = "";
      titleEl.removeAttribute("href");
      bylineEl.textContent = "";
      return;
    }
    titleEl.textContent = item.title || "(untitled)";
    titleEl.href = item.permalink || item.src;
    const sub = item.subreddit ? `r/${item.subreddit}` : "";
    const user = item.author ? `u/${item.author}` : "";
    const score = item.score ? `${item.score} upvotes` : "";
    bylineEl.textContent = [sub, user, score].filter(Boolean).join(" · ");
  }

  function visibleVideos() {
    return [...feed.querySelectorAll("video")].filter((video) => {
      const rect = video.getBoundingClientRect();
      return rect.bottom > 40 && rect.top < window.innerHeight - 40;
    });
  }

  function columnsNeedMore() {
    if (isSingle()) return false;
    const cols = colEls();
    if (!cols.length) return true;
    return cols.some((col) => col.scrollHeight <= col.clientHeight + 24);
  }

  function stopColScroll() {
    if (state.scrollRaf) {
      cancelAnimationFrame(state.scrollRaf);
      state.scrollRaf = 0;
    }
  }

  function pinSlide(slide) {
    if (!slide || slide.dataset.pinned) return;
    const height = slide.getBoundingClientRect().height;
    if (height > 8) {
      slide.style.minHeight = `${Math.round(height)}px`;
      slide.dataset.pinned = "1";
    }
  }

  function mediaLoaded(el) {
    return Boolean(el && el.getAttribute("src"));
  }

  function attachMedia(slide, playVideo) {
    const video = slide.querySelector("video");
    const img = slide.querySelector("img");
    if (video && video.dataset.src && video.getAttribute("src") !== video.dataset.src) {
      video.src = video.dataset.src;
      if (video.dataset.poster) video.poster = video.dataset.poster;
      video.load();
    }
    if (img && img.dataset.src && img.getAttribute("src") !== img.dataset.src) {
      img.src = img.dataset.src;
    }
    if (playVideo && video) video.play().catch(() => {});
  }

  function releaseMedia(slide) {
    const video = slide.querySelector("video");
    const img = slide.querySelector("img");
    if (!mediaLoaded(video) && !mediaLoaded(img)) return;
    pinSlide(slide);
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.removeAttribute("poster");
      try {
        video.load();
      } catch {
        /* ignore */
      }
    }
    if (img) {
      img.removeAttribute("src");
      img.src = "";
    }
  }

  function hydrateAround() {
    const keep = isSingle() ? window.innerHeight * 1.1 : Math.round(window.innerHeight * 0.75);
    const drop = keep * 2;
    colEls().forEach((col) => {
      const viewTop = col.scrollTop;
      const viewBottom = viewTop + col.clientHeight;
      col.querySelectorAll(".slide").forEach((slide) => {
        const top = slide.offsetTop;
        const bottom = top + (slide.offsetHeight || 1);
        const onscreen = bottom > viewTop && top < viewBottom;
        if (bottom >= viewTop - keep && top <= viewBottom + keep) {
          attachMedia(slide, onscreen);
        } else if (bottom < viewTop - drop || top > viewBottom + drop) {
          releaseMedia(slide);
        }
      });
      if (viewBottom > col.scrollHeight - col.clientHeight * 1.8) loadPage();
    });
  }

  function startColScroll() {
    if (isSingle() || !state.playing) return;
    if (state.scrollRaf) cancelAnimationFrame(state.scrollRaf);
    let last = performance.now();
    let frames = 0;
    const step = (now) => {
      if (!state.playing || isSingle()) {
        state.scrollRaf = 0;
        return;
      }
      const dt = Math.min(50, now - last);
      last = now;
      if (Date.now() >= state.userScrollUntil) {
        colEls().forEach((col, i) => {
          const motion = state.colDirs[i] || { speed: 1, acc: 0 };
          const pxPerSec = Number(state.speed);
          if (!Number.isFinite(pxPerSec) || pxPerSec === 0) return;
          motion.acc = (motion.acc || 0) + (pxPerSec * (motion.speed || 1) * dt) / 1000;
          const jump = Math.trunc(motion.acc);
          if (jump !== 0) {
            col.scrollTop += jump;
            motion.acc -= jump;
          }
          const max = col.scrollHeight - col.clientHeight;
          if (max <= 8) {
            loadPage();
            return;
          }
          if (col.scrollTop >= max - 2) {
            loadPage();
            if (state.exhausted) col.scrollTop = 0;
          }
        });
      }
      frames += 1;
      if (frames % 12 === 0) hydrateAround();
      state.scrollRaf = requestAnimationFrame(step);
    };
    state.scrollRaf = requestAnimationFrame(step);
    hydrateAround();
  }

  function setPlaying(on) {
    state.playing = on;
    playBtn.textContent = on ? "❚❚" : "▶";
    if (on) {
      if (isSingle()) queueAdvance();
      else startColScroll();
    } else {
      clearTimeout(state.slideTimer);
      stopColScroll();
      hydrateAround();
    }
    savePrefs();
  }

  function setMuted(on) {
    state.muted = on;
    muteBtn.textContent = on ? "muted" : "sound";
    feed.querySelectorAll("video").forEach((video) => {
      video.muted = on;
    });
    savePrefs();
  }

  function activate(index, { scroll = false } = {}) {
    if (!state.items.length) return;
    state.index = Math.max(0, Math.min(index, state.items.length - 1));
    feed.querySelectorAll(".slide.active").forEach((el) => el.classList.remove("active"));
    const el = slideEl(state.index);
    if (el) {
      el.classList.add("active");
      if (scroll) {
        el.scrollIntoView({
          behavior: "smooth",
          block: isSingle() ? "start" : "nearest",
          inline: "nearest",
        });
      }
    }
    visibleVideos().forEach((video) => video.play().catch(() => {}));
    updateMeta();
    if (isSingle()) queueAdvance();
    if (state.index >= state.items.length - 3) loadPage();
  }

  function queueAdvance() {
    clearTimeout(state.slideTimer);
    const item = state.items[state.index];
    if (!state.playing || !item || !isSingle()) return;
    if (item.type === "video") return;
    const stills = Math.max(1.5, 20 / Math.max(1, state.speed));
    state.slideTimer = setTimeout(() => next(), stills * 1000);
  }

  function next() {
    if (state.index < state.items.length - 1) {
      activate(state.index + 1, { scroll: true });
      return;
    }
    if (!state.exhausted) {
      setStatus("Loading more…");
      loadPage().then(() => {
        if (state.index < state.items.length - 1) activate(state.index + 1, { scroll: true });
        else if (state.exhausted && state.items.length) activate(0, { scroll: true });
      });
      return;
    }
    if (state.items.length) activate(0, { scroll: true });
  }

  function prev() {
    if (state.index > 0) activate(state.index - 1, { scroll: true });
    else if (state.items.length) activate(state.items.length - 1, { scroll: true });
  }

  function lockMediaSize(el) {
    const w = el.naturalWidth || el.videoWidth;
    const h = el.naturalHeight || el.videoHeight;
    if (!w || !h) return;
    el.style.aspectRatio = `${w} / ${h}`;
  }

  function markFailed(index) {
    const el = slideEl(index);
    if (!el || el.querySelector(".fail")) return;
    el.innerHTML = `<p class="fail">Couldn't play this one</p>`;
    if (index === state.index && state.playing && isSingle()) next();
  }

  function observeMedia(slide) {
    if (!state.mediaObserver) {
      state.mediaObserver = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) {
            if (entry.isIntersecting) attachMedia(entry.target, true);
            else entry.target.querySelector("video")?.pause();
          }
        },
        { root: null, rootMargin: "120px 0px", threshold: 0.01 }
      );
    }
    state.mediaObserver.observe(slide);
  }

  function makeSlide(i) {
    const item = state.items[i];
    const slide = document.createElement("section");
    slide.className = "slide";
    slide.dataset.index = String(i);
    slide.title = item.title || "";
    const frame = document.createElement("div");
    frame.className = "frame";
    if (item.type === "video") {
      const video = document.createElement("video");
      video.playsInline = true;
      video.loop = !isSingle();
      video.preload = "none";
      video.muted = state.muted;
      video.controls = false;
      video.dataset.src = item.src;
      if (item.poster) video.dataset.poster = item.poster;
      video.addEventListener("loadedmetadata", () => {
        lockMediaSize(video);
        pinSlide(slide);
      });
      video.addEventListener("ended", () => {
        if (isSingle() && Number(slide.dataset.index) === state.index && state.playing) next();
      });
      video.addEventListener("error", () => markFailed(i));
      frame.appendChild(video);
    } else {
      const img = document.createElement("img");
      img.alt = item.title || "";
      img.decoding = "async";
      img.dataset.src = item.src;
      img.addEventListener("load", () => {
        lockMediaSize(img);
        pinSlide(slide);
      });
      img.addEventListener("error", () => markFailed(i));
      frame.appendChild(img);
    }
    const link = document.createElement("a");
    link.className = "post-link";
    link.href = item.permalink || item.src || "#";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = item.title ? `${item.title} — open on Reddit` : "Open Reddit post";
    link.appendChild(frame);
    slide.appendChild(link);
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = item.subreddit ? `r/${item.subreddit}` : "";
    if (tag.textContent) {
      const tagLink = document.createElement("a");
      tagLink.className = "tag";
      tagLink.href = link.href;
      tagLink.target = "_blank";
      tagLink.rel = "noopener noreferrer";
      tagLink.textContent = tag.textContent;
      slide.appendChild(tagLink);
    }
    slide.addEventListener("click", () => activate(i, { scroll: false }));
    return slide;
  }

  function renderItems(startAt) {
    const n = Math.max(1, colEls().length || columnCount());
    if (!colEls().length) buildColumns();
    const cols = colEls();
    for (let i = startAt; i < state.items.length; i += 1) {
      if (feed.querySelector(`[data-index="${i}"]`)) continue;
      const slide = makeSlide(i);
      cols[i % n].appendChild(slide);
      observeMedia(slide);
    }
    emptyEl.classList.toggle("hidden", state.items.length > 0);
  }

  function shuffleArray(list) {
    for (let i = list.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
  }

  function subKey(item) {
    const name = (item.subreddit || "").toLowerCase();
    if (state.streams.has(name)) return name;
    const match = [...state.streams.keys()].find((key) => key === name || key.endsWith(`:${name}`) || key.split(":")[0] === name);
    if (match) return match;
    if (state.subs.length === 1) return state.subs[0].toLowerCase();
    return name || "other";
  }

  function resetStreams() {
    state.streams = new Map();
    state.streamPos = new Map();
    for (const sub of state.subs) {
      const key = sub.toLowerCase();
      state.streams.set(key, []);
      state.streamPos.set(key, 0);
    }
    state.streams.set("other", []);
    state.streamPos.set("other", 0);
  }

  function take(key) {
    const pos = state.streamPos.get(key) || 0;
    const list = state.streams.get(key) || [];
    const item = list[pos];
    state.streamPos.set(key, pos + 1);
    return item;
  }

  function remaining(key) {
    return (state.streams.get(key) || []).length - (state.streamPos.get(key) || 0);
  }

  function flushRows() {
    const added = [];
    const keys = [...state.subs.map((sub) => sub.toLowerCase()), "other"];
    while (keys.some((key) => remaining(key) > 0)) {
      let took = false;
      for (const key of keys) {
        if (remaining(key) > 0) {
          added.push(take(key));
          took = true;
        }
      }
      if (!took) break;
    }
    return added;
  }

  async function resolveLazy(item) {
    if (item.type === "redgifs") {
      const id = (item.src.match(/(?:watch|ifr)\/([A-Za-z0-9]+)/i) || item.src.match(/([A-Za-z0-9]+)$/) || [])[1];
      if (!id) {
        item.failed = true;
        return item;
      }
      try {
        const res = await fetch(`/api/redgifs?id=${encodeURIComponent(id)}`);
        const data = await res.json();
        if (data.src) {
          item.type = "video";
          item.src = data.src;
          item.poster = data.poster || item.poster || "";
        } else item.failed = true;
      } catch {
        item.failed = true;
      }
      return item;
    }
    if (item.type === "vreddit") {
      try {
        const res = await fetch(`/api/vreddit?id=${encodeURIComponent(item.src)}`);
        const data = await res.json();
        if (data.src) {
          item.type = "video";
          item.src = data.src;
        } else item.failed = true;
      } catch {
        item.failed = true;
      }
    }
    return item;
  }

  async function fetchSource(src) {
    if (src.exhausted) return [];
    if (src.kind === "reddit") {
      const params = new URLSearchParams({
        sub: src.query,
        sort: state.query.sort,
        t: state.query.t,
        limit: String(PAGE_SIZE),
      });
      if (src.after) params.set("after", src.after);
      const res = await fetch(`/api/listing?${params}`);
      const data = await res.json();
      if (res.status === 429) {
        src.retry = true;
        throw Object.assign(new Error(data.error || "rate limited"), { retry: Number(data.retry_after || 60) });
      }
      if (!res.ok) throw new Error(data.error || `Reddit error ${res.status}`);
      src.after = data.after || null;
      if (!src.after) src.exhausted = true;
      state.source = data.source || state.source;
      return (data.items || []).map((item) => item);
    }
    const params = new URLSearchParams({
      site: src.kind,
      tags: src.query,
      page: String(src.page || 0),
      limit: String(PAGE_SIZE),
    });
    const res = await fetch(`/api/booru?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `${src.kind} error ${res.status}`);
    const items = (data.items || []).map((item) => ({
      ...item,
      subreddit: src.label,
    }));
    src.page = (src.page || 0) + 1;
    if (!items.length) src.exhausted = true;
    return items;
  }

  async function loadPage() {
    if (state.loading || state.exhausted) return;
    state.loading = true;
    setStatus(`Fetching ${state.subs.join(", ")}…`);
    try {
      const incoming = [];
      for (const src of state.sources) {
        try {
          incoming.push(...(await fetchSource(src)));
        } catch (err) {
          if (err.retry) {
            setStatus(`Rate-limited. Retrying in ${err.retry}s…`);
            state.loading = false;
            await new Promise((resolve) => setTimeout(resolve, Math.min(err.retry, 90) * 1000));
            return loadPage();
          }
          setStatus(err.message || "Fetch failed");
        }
      }
      let accepted = 0;
      const fresh = [];
      for (const raw of incoming) {
        const key = `${raw.postId}:${raw.src}`;
        if (state.seen.has(key)) continue;
        const item = await resolveLazy(raw);
        if (item.failed || !item.src) continue;
        const resolvedKey = `${item.postId}:${item.src}`;
        if (state.seen.has(resolvedKey)) continue;
        state.seen.add(key);
        state.seen.add(resolvedKey);
        accepted += 1;
        fresh.push(item);
      }
      if (state.shuffle) shuffleArray(fresh);
      for (const item of fresh) {
        const key = subKey(item);
        if (!state.streams.has(key)) {
          state.streams.set(key, []);
          state.streamPos.set(key, 0);
        }
        state.streams.get(key).push(item);
      }
      state.emptyPages = accepted === 0 ? state.emptyPages + 1 : 0;
      if (state.sources.every((src) => src.exhausted) || state.emptyPages >= 3) {
        state.exhausted = true;
      }
      const startAt = state.items.length;
      const rows = flushRows();
      state.items.push(...rows);
      renderItems(startAt);
      updateMeta();
      setStatus(
        state.exhausted
          ? `${state.items.length} media · end of listing`
          : `${state.items.length} media`
      );
      if (startAt === 0 && state.items.length) activate(0);
      if ((accepted === 0 || rows.length === 0) && !state.exhausted) {
        state.loading = false;
        return loadPage();
      }
      if (!isSingle() && columnsNeedMore() && !state.exhausted && state.fillPasses < 8) {
        state.fillPasses += 1;
        state.loading = false;
        return loadPage();
      }
    } catch (err) {
      setStatus("Could not reach the local proxy. Run start.bat.");
      console.error(err);
    }
    state.loading = false;
    if (state.playing && !isSingle()) startColScroll();
  }

  async function startFeed() {
    const raw = $("subs").value.trim() || "pics";
    $("subs").value = raw;
    const parsed = parseSources(raw);
    const redditNames = parsed.filter((src) => src.kind === "reddit").map((src) => src.query);
    const boorus = parsed.filter((src) => src.kind !== "reddit");
    state.sources = [];
    if (redditNames.length) {
      state.sources.push({
        kind: "reddit",
        query: redditNames.join("+"),
        label: redditNames.join("+"),
        after: null,
        exhausted: false,
      });
    }
    boorus.forEach((src) => state.sources.push({ ...src, page: 0, exhausted: false }));
    state.subs = parsed.map((src) => src.label);
    state.cols = $("cols").value || "auto";
    state.query = {
      sub: redditNames.join("+"),
      sort: $("sort").value,
      t: $("time").value,
    };
    state.items = [];
    state.after = null;
    state.exhausted = false;
    state.index = 0;
    state.seen.clear();
    state.emptyPages = 0;
    state.fillPasses = 0;
    resetStreams();
    stopColScroll();
    if (state.mediaObserver) {
      state.mediaObserver.disconnect();
      state.mediaObserver = null;
    }
    state.playing = true;
    playBtn.textContent = "❚❚";
    buildColumns();
    savePrefs();
    await loadPage();
    if (!isSingle()) startColScroll();
    feed.focus();
  }

  function dimHudSoon() {
    clearTimeout(state.hideTimer);
    document.querySelectorAll(".hud, .dock, .meta").forEach((el) => el.classList.remove("dim"));
    state.hideTimer = setTimeout(() => {
      document.querySelectorAll(".hud, .dock, .meta").forEach((el) => el.classList.add("dim"));
    }, 2800);
  }

  function applyPrefs() {
    const prefs = loadPrefs();
    $("subs").value = prefs.subs || "pics, gifs, art";
    $("sort").value = prefs.sort || "hot";
    $("time").value = prefs.t || "day";
    $("cols").value = prefs.cols || "auto";
    state.cols = $("cols").value;
    state.speed = Number(prefs.speed);
    if (!Number.isFinite(state.speed)) state.speed = SPEED_DEFAULT;
    speedInput.value = String(state.speed);
    $("shuffle").checked = Boolean(prefs.shuffle);
    state.shuffle = Boolean(prefs.shuffle);
    setMuted(prefs.muted !== false);
    state.playing = prefs.playing !== false;
    playBtn.textContent = state.playing ? "❚❚" : "▶";
    $("time").disabled = $("sort").value !== "top";
    if (prefs.entered) $("gate").classList.add("hidden");
    renderPresets();
  }

  $("enter").addEventListener("click", () => {
    $("gate").classList.add("hidden");
    savePrefs();
    startFeed();
  });

  $("load-form").addEventListener("submit", (event) => {
    event.preventDefault();
    startFeed();
  });

  $("sort").addEventListener("change", () => {
    $("time").disabled = $("sort").value !== "top";
  });

  $("presets").addEventListener("change", () => {
    const id = $("presets").value;
    const preset = loadPresetList().find((row) => row.id === id);
    if (!preset) return;
    $("subs").value = preset.sources;
    if (preset.cols) $("cols").value = preset.cols;
    startFeed();
  });

  $("save-preset").addEventListener("click", () => {
    const sources = $("subs").value.trim();
    if (!sources) return;
    const name = window.prompt("Preset name", sources.slice(0, 40));
    if (!name) return;
    const list = loadPresetList();
    list.push({
      id: `${Date.now()}`,
      name: name.trim(),
      sources,
      cols: $("cols").value,
    });
    savePresetList(list);
    renderPresets();
    $("presets").value = list[list.length - 1].id;
  });

  $("del-preset").addEventListener("click", () => {
    const id = $("presets").value;
    if (!id) return;
    savePresetList(loadPresetList().filter((row) => row.id !== id));
    renderPresets();
  });

  $("save-booru").addEventListener("click", async () => {
    try {
      await fetch("/api/booru-creds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          gelbooru_user_id: $("gelbooru-user").value.trim(),
          gelbooru_api_key: $("gelbooru-key").value.trim(),
          r34_user_id: $("r34-user").value.trim(),
          r34_api_key: $("r34-key").value.trim(),
        }),
      });
      apiState.textContent = "Saved Gelbooru / rule34 keys.";
    } catch {
      apiState.textContent = "Could not save booru keys.";
    }
  });

  $("cols").addEventListener("change", () => {
    state.cols = $("cols").value;
    savePrefs();
    applyColumns();
    if (state.playing && !isSingle()) startColScroll();
    if (state.items.length) activate(state.index, { scroll: true });
    if (!isSingle() && columnsNeedMore() && !state.exhausted) loadPage();
  });

  $("play").addEventListener("click", () => setPlaying(!state.playing));
  $("next").addEventListener("click", next);
  $("prev").addEventListener("click", prev);
  $("mute").addEventListener("click", () => setMuted(!state.muted));
  $("shuffle").addEventListener("change", (event) => {
    state.shuffle = event.target.checked;
    savePrefs();
  });
  function readSpeed() {
    const value = Number(speedInput.value);
    if (!Number.isFinite(value) || value < 0) return;
    state.speed = value;
    savePrefs();
    queueAdvance();
  }
  speedInput.addEventListener("input", readSpeed);
  speedInput.addEventListener("change", readSpeed);

  $("api-btn").addEventListener("click", async () => {
    await refreshApiStatus();
    $("api-modal").showModal();
  });

  $("connect").addEventListener("click", async () => {
    const client_id = $("client-id").value.trim();
    const client_secret = $("client-secret").value.trim();
    if (!client_id) {
      apiState.textContent = "Paste a Reddit client id first.";
      return;
    }
    try {
      const res = await fetch("/api/creds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, client_secret }),
      });
      const data = await res.json();
      if (!res.ok) {
        apiState.textContent = data.error || "Could not save credentials.";
        return;
      }
      window.location.href = data.redirect;
    } catch {
      apiState.textContent = "Local proxy is not running.";
    }
  });

  $("disconnect").addEventListener("click", async () => {
    await fetch("/api/disconnect", { method: "POST" });
    await refreshApiStatus();
  });

  document.addEventListener("mousemove", dimHudSoon);
  document.addEventListener("click", dimHudSoon);

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, select, textarea")) return;
    if (event.key === " ") {
      event.preventDefault();
      setPlaying(!state.playing);
    } else if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      next();
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      prev();
    } else if (event.key === "m" || event.key === "M") {
      setMuted(!state.muted);
    } else if (event.key === "f" || event.key === "F") {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
      else document.exitFullscreen().catch(() => {});
    }
  });

  applyPrefs();
  dimHudSoon();
  emptyEl.classList.remove("hidden");
  refreshApiStatus().then(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("oauth") === "ok") {
      $("gate").classList.add("hidden");
      history.replaceState({}, "", "/");
      startFeed();
      return;
    }
    if (params.get("oauth") === "error") {
      $("gate").classList.add("hidden");
      $("api-modal").showModal();
      apiState.textContent = "Reddit login failed. Check the redirect URI and client id.";
      history.replaceState({}, "", "/");
    }
    if (loadPrefs().entered) startFeed();
  });
})();
