"use strict";
/* Content HQ front end. All dynamic text goes through textContent (never innerHTML). */

const $app = document.getElementById("app");
let ST = null; // server state: settings, labels, keys

// ---------- helpers ----------
function el(tag, props, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "disabled" || k === "value") n[k] = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
}
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);
const link = (u, text) => (safeUrl(u) ? el("a", { href: u, target: "_blank", rel: "noopener noreferrer", text: text || "Open source" }) : null);
const fmt = (n) => (n == null ? "n/a" : Number(n) >= 1e6 ? (n / 1e6).toFixed(1) + "M" : Number(n) >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(Math.round(n)));
const dateOnly = (iso) => (iso ? iso.slice(0, 10) : "n/a");

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.json !== undefined) { o.method = o.method || "POST"; o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.json); }
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("login"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Something went wrong.");
  return data;
}
let toastTimer;
function toast(msg, bad) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = el("div", { class: "toast" + (bad ? " err" : ""), role: "status", text: msg });
  document.body.append(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 3200);
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch { const ta = el("textarea", { value: text }); document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
  const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = old), 1200);
}
const initials = (name) => (name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
function greeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }

function toggle(checked, onChange, label) {
  const b = el("button", { class: "switch", role: "switch", "aria-checked": String(!!checked), "aria-label": label, type: "button" });
  b.addEventListener("click", () => { const v = b.getAttribute("aria-checked") !== "true"; b.setAttribute("aria-checked", String(v)); onChange(v); });
  return b;
}
function field(label, input) { return el("label", { class: "f" }, label, input); }

// ---------- shell ----------
const NAV = [["brief", "Daily Brief"], ["hooks", "Hook Bank"], ["competitors", "Competitors"], ["sources", "Sources"], ["pictures", null], ["strategy", "Strategy"], ["history", "History"], ["settings", "Settings"]];

function shell(route, content) {
  const p = ST.settings.profile, sc = ST.settings.schedule;
  const nav = el("nav", { "aria-label": "Main" }, NAV.map(([id, label]) =>
    el("a", { href: "#/" + id, class: route === id ? "on" : "", "aria-current": route === id ? "page" : false, text: id === "pictures" ? ST.pictures_label : label })));
  const side = el("aside", { class: "side" },
    el("div", { class: "brand" }, el("div", { class: "logo", text: initials(p.creator_name) }),
      el("div", {}, el("div", { class: "brandname", text: p.brand_name }), el("div", { class: "brandsub", text: "Content HQ" }))),
    nav,
    el("div", { class: "chip" }, el("span", { class: "dot" + (sc.enabled ? "" : " off") }),
      sc.enabled ? `Agent live, daily at ${sc.time}` : "Agent paused"));
  $app.replaceChildren(el("div", { class: "shell" }, side, el("main", { class: "main" }, content)));
}

function head(title, sub, ...actions) {
  return el("div", { class: "head" }, el("div", {}, el("h1", { text: title }), sub ? el("div", { class: "sub muted", text: sub }) : null), el("div", { class: "row" }, actions));
}
const empty = (title, text, ...actions) => el("div", { class: "empty" }, el("h3", { text: title }), el("p", { text }), el("div", { class: "row", style: "justify-content:center" }, actions));

// ---------- login ----------
function showLogin() {
  const input = el("input", { type: "password", "aria-label": "Password", autocomplete: "current-password" });
  const go = async () => { try { await api("/api/login", { json: { password: input.value } }); boot(); } catch (e) { if (e.message !== "login") toast(e.message, true); } };
  input.addEventListener("keydown", (e) => e.key === "Enter" && go());
  $app.replaceChildren(el("div", { class: "login" }, el("h1", { text: "Content HQ" }), el("p", { class: "muted", text: "Enter the password to continue." }),
    el("div", { class: "form" }, input, el("button", { class: "btn primary", onclick: go, text: "Sign in" }))));
  input.focus();
}

// ---------- Daily Brief ----------
let pollTimer = null;
let selKey = null; // remembers which piece of the brief is open
const HINDI_RULE = "Write all spoken content (scripts, hooks, captions, titles) in Hindi (Devanagari script) mixed with common English tech words, the way people really talk. Use only Devanagari and English (Latin) letters, never Arabic or Persian script, and prefer everyday Hindi or English words over Persian or Arabic-derived ones.";
async function pageBrief(id) {
  const [latest, list] = await Promise.all([id ? api("/api/briefs/" + id) : api("/api/briefs/latest"), api("/api/briefs")]);
  const sources = await api("/api/sources");
  const p = ST.settings.profile;
  const runBtn = el("button", { class: "btn primary", text: "Generate brief now" });
  const log = el("div", { class: "log", "aria-live": "polite" });
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true; runBtn.textContent = "Working…"; log.textContent = "";
    await api("/api/run", { method: "POST" });
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const s = await api("/api/run/status");
      log.textContent = s.log.join("\n");
      if (!s.running) {
        clearInterval(pollTimer); runBtn.disabled = false; runBtn.textContent = "Generate brief now";
        if (s.error) toast(s.error, true); else { toast("Brief ready"); route(); }
      }
    }, 900);
  });

  const parts = [head(`${greeting()}, ${p.creator_name}`,
    latest ? `Brief for ${latest.day}` : "No brief yet", runBtn), log];
  if (ST.mode === "demo") parts.push(el("div", { class: "banner", text: "Demo mode: no AI key is set, so briefs are samples. Add ANTHROPIC_API_KEY to the .env file for real briefs in your voice." }));

  // last 7 days strip (functional: days with a brief are clickable)
  const byDay = {}; list.forEach((b) => { if (!byDay[b.day]) byDay[b.day] = b.id; });
  const strip = el("div", { class: "days", role: "group", "aria-label": "Last 7 days" });
  for (let i = 6; i >= 0; i--) {
    const d = new Date(); d.setDate(d.getDate() - i);
    const key = d.toLocaleDateString("en-CA"); const bid = byDay[key];
    const on = latest && latest.day === key;
    strip.append(el(bid ? "button" : "div", { class: "day" + (bid ? " has" : "") + (on ? " on" : ""), onclick: bid ? () => (location.hash = "#/brief/" + bid) : null,
      text: d.toLocaleDateString("en-US", { weekday: "short" }) + (i === 0 ? " (today)" : "") }));
  }
  parts.push(strip);

  if (!latest) {
    parts.push(el("div", { class: "panel" }, empty("Your first brief is one click away",
      "Add a few sources, then generate. Want to explore first? Load sample data.",
      el("a", { class: "btn", href: "#/sources", text: "Add sources" }),
      el("button", { class: "btn", text: "Load sample data", onclick: async () => { await api("/api/demo/seed", { method: "POST" }); toast("Sample data loaded"); route(); } }))));
    shell("brief", parts); return;
  }
  const b = latest.data;
  parts.push(el("div", { class: "panel" }, el("h2", { text: "Trending today" }), el("div", { style: "height:12px" }),
    b.trending && b.trending.length ? el("ol", { class: "trend" }, b.trending.map((t, i) => el("li", {},
      el("span", { class: "rank", text: i + 1 }),
      el("div", {}, el("div", { text: t.topic }), el("div", { class: "muted small", text: t.why_it_matters || "" }),
        el("div", { class: "small" }, t.source_url ? [link(t.source_url, "Source"), " ", el("span", { class: "badge ok", text: "link checked" })] : el("span", { class: "badge err", text: "no verified source" })))))) : el("p", { class: "muted", text: "Nothing trending was found." })));
  if (b.notes && latest.mode !== "demo") parts.push(el("div", { class: "banner", text: b.notes }));

  // reader
  const pieces = [];
  (b.reels || []).forEach((r, i) => pieces.push({ key: "reel" + i, section: "reel" + i, name: `Reel ${i + 1}`, sub: r.type === "news" ? "News" : "Tool or tutorial",
    fields: [["Topic", r.topic], ["Hook strategy", r.hook_strategy], ["Hook", r.hook], ["Thumbnail concept", r.thumbnail], ["Caption", r.caption], ["Hashtags", (r.hashtags || []).join(" ")], ["Script", r.script], ["Call to action", r.cta]], src: r.source_url }));
  if (b.long_video) { const v = b.long_video; pieces.push({ key: "lv", section: "long_video", name: "Long video", sub: v.category || "",
    fields: [["Title", v.title], ["Title in English", v.subtitle], ["Thumbnail concept", v.thumbnail], ...(v.outline || []).map((o) => [o.section + (o.time ? " (" + o.time + ")" : ""), o.dialogue])], src: v.source_url }); }
  if (b.article) { const a = b.article; pieces.push({ key: "ar", section: "article", name: "Article", sub: a.platform || "",
    fields: [["Title", a.title], ["Reading time", a.reading_time], ["Opening hook", a.opening_hook], ["Key sections", (a.sections || []).map((s) => "• " + s).join("\n")], ["Closing line", a.closing_line]], src: a.source_url }); }
  pieces.push({ key: "bk", name: "Backup ideas", sub: "If today's don't fit", fields: (b.backup_ideas || []).map((x, i) => ["Idea " + (i + 1), x]) });
  let cur = pieces.find((pc) => pc.key === selKey) || pieces[0];
  const detail = el("div", { class: "panel", "aria-live": "polite" });
  const list2 = el("div", { class: "pieces" });
  const regen = async (btn) => {
    btn.disabled = true; btn.textContent = "Rewriting…";
    try { await api(`/api/briefs/${latest.id}/regenerate`, { json: { section: cur.section } }); toast("Section rewritten"); route(); }
    catch (e) { if (e.message !== "login") toast(e.message, true); btn.disabled = false; btn.textContent = "Rewrite this section"; }
  };
  const draw = () => {
    list2.replaceChildren(...pieces.map((pc) => el("button", { class: "piece" + (pc === cur ? " on" : ""), onclick: () => { cur = pc; selKey = pc.key; draw(); } }, pc.name, el("small", { text: pc.sub }))));
    const rb = cur.section ? el("button", { class: "btn small", text: "Rewrite this section" }) : null;
    if (rb) rb.addEventListener("click", () => regen(rb));
    detail.replaceChildren(el("div", { class: "row", style: "justify-content:space-between" }, el("h2", { text: cur.name }), rb), ...cur.fields.filter(([, v]) => v).map(([k, v]) => {
      const btn = el("button", { class: "copy", text: "Copy" }); btn.addEventListener("click", () => copyText(v, btn));
      return el("div", { class: "fb" }, el("div", { class: "lab" }, k, btn), el("div", { class: "val", text: v }));
    }), cur.src ? el("div", { class: "fb" }, el("div", { class: "lab", text: "Source" }), link(cur.src)) : null);
  };
  draw();
  parts.push(el("div", { class: "reader" }, list2, detail));

  // source health
  const act = sources.filter((s) => s.active);
  parts.push(el("div", { class: "panel" }, el("h2", { text: "Source health" }), el("div", { style: "height:10px" }),
    act.length ? el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Source", "Type", "Last run", "Items", "Status"].map((h) => el("th", { text: h })))),
      el("tbody", {}, act.map((s) => el("tr", {}, el("td", { text: s.name }), el("td", { text: s.type }), el("td", { text: dateOnly(s.last_run) }), el("td", { text: s.last_count ?? 0 }),
        el("td", {}, el("span", { class: "badge " + (s.last_status === "ok" ? "ok" : s.last_status === "error" ? "err" : "off"), title: s.last_error || "", text: s.last_status || "not run" }))))))) : el("p", { class: "muted", text: "No active sources." })));
  shell("brief", parts);
}

// ---------- Hook Bank ----------
async function pageHooks() {
  const hooks = await api("/api/hooks");
  const text = el("textarea", { "aria-label": "Hook", placeholder: "Paste a hook that worked for someone else" });
  const tag = el("input", { type: "text", placeholder: "Tag (optional), e.g. curiosity", "aria-label": "Tag" });
  const add = async () => { try { await api("/api/hooks", { json: { text: text.value, tag: tag.value } }); route(); } catch (e) { toast(e.message, true); } };
  shell("hooks", [head("Hook Bank", "Openers worth borrowing. The 10 newest are shown to the AI as style inspiration."),
    el("div", { class: "panel form" }, text, el("div", { class: "grid2" }, tag, el("div", {}, el("button", { class: "btn primary", text: "Save hook", onclick: add })))),
    el("div", { class: "panel" }, hooks.length ? el("div", {}, hooks.map((h) => el("div", { class: "fb" }, el("div", { class: "lab" }, h.tag ? el("span", { class: "badge", text: h.tag }) : el("span"),
      el("button", { class: "copy", text: "Delete", onclick: async () => { await api("/api/hooks/" + h.id, { method: "DELETE" }); route(); } })), el("div", { class: "val", text: h.text })))) : empty("No hooks saved", "Save hooks you notice while scrolling. They make your scripts sharper."))]);
}

// ---------- Competitors ----------
let platFilter = "";
async function pageCompetitors() {
  const items = await api("/api/items" + (platFilter ? "?platform=" + platFilter : ""));
  const chips = el("div", { class: "chips", role: "group", "aria-label": "Platform filter" }, [["", "All"], ["youtube", "YouTube"], ["instagram", "Instagram"], ["facebook", "Facebook"], ["x", "X"], ["news", "News"]].map(([v, l]) =>
    el("button", { class: "fchip" + (platFilter === v ? " on" : ""), text: l, onclick: () => { platFilter = v; route(); } })));
  const body = items.length ? el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Account", "Post", "Result", "vs. usual", "Date", ""].map((h) => el("th", { text: h })))),
    el("tbody", {}, items.map((i) => el("tr", {}, el("td", { text: i.account }), el("td", { text: i.title }),
      el("td", { class: "nw", text: i.value == null ? "n/a" : `${fmt(i.value)} ${i.metric || ""}` }),
      el("td", {}, i.outlier == null ? el("span", { class: "muted", text: "n/a" }) : i.reliable === false ? el("span", { class: "muted", title: "Too few views or likes to trust this ratio", text: i.outlier + "x (low volume)" }) : el("span", { class: "badge " + (i.outlier >= 5 ? "hot" : i.outlier >= 2.5 ? "warm" : ""), text: i.outlier + "x" })),
      el("td", { class: "nw", text: dateOnly(i.published) }), el("td", {}, link(i.url, "Open"))))))) : empty("Nothing collected yet", "Add sources and generate a brief, and posts will appear here ranked by how far they beat the account's usual result.", el("a", { class: "btn", href: "#/sources", text: "Add sources" }));
  shell("competitors", [head("Competitors", "Posts ranked by how far they beat that account's own median. 2.5x is above average, 5x is an outlier."), chips, el("div", { class: "panel" }, body)]);
}

// ---------- Sources ----------
async function pageSources() {
  const list = await api("/api/sources");
  let editing = null;
  const name = el("input", { type: "text", "aria-label": "Name", placeholder: "Display name" });
  const type = el("select", { "aria-label": "Type" }, [["youtube", "YouTube channel"], ["news", "News or newsletter (RSS)"], ["instagram", "Instagram account"], ["facebook", "Facebook Page"], ["x", "X account (optional)"]].map(([v, l]) => el("option", { value: v, text: l })));
  const url = el("input", { type: "text", "aria-label": "URL or handle", placeholder: "Channel ID (UC…), feed URL, or @handle" });
  const hint = el("div", { class: "muted small", text: "" });
  const hints = { youtube: "Use the channel ID (starts with UC) or, if YOUTUBE_API_KEY is set, an @handle.", news: "Paste the RSS feed URL. For Substack: https://name.substack.com/feed", instagram: "Public account handle. Needs APIFY_TOKEN.", facebook: "Public Page link, like https://www.facebook.com/PageName. Needs APIFY_TOKEN.", x: "Handle. Off by default in Settings, and billed per post read." };
  type.addEventListener("change", () => (hint.textContent = hints[type.value])); hint.textContent = hints.youtube;
  const save = el("button", { class: "btn primary", text: "Add source" });
  save.addEventListener("click", async () => {
    try {
      const body = { name: name.value, type: type.value, url: url.value, active: true };
      if (editing) await api("/api/sources/" + editing.id, { method: "PUT", json: { ...body, active: !!editing.active } }); else await api("/api/sources", { json: body });
      route();
    } catch (e) { toast(e.message, true); }
  });
  const rows = list.map((s) => el("tr", {},
    el("td", { text: s.name + (s.is_demo ? " (sample)" : "") }), el("td", { text: s.type }), el("td", { class: "muted", text: s.url }),
    el("td", {}, toggle(s.active, async (v) => { await api("/api/sources/" + s.id, { method: "PUT", json: { name: s.name, type: s.type, url: s.url, active: v } }); }, "Active")),
    el("td", {}, (() => { const tb = el("button", { class: "btn small", text: "Test" }); tb.addEventListener("click", async () => {
        tb.disabled = true; tb.textContent = "Testing…";
        try { const r = await api("/api/sources/" + s.id + "/test", { method: "POST" });
          toast(r.ok ? `Works: ${r.count} posts. Example: ${r.sample[0] || ""}` : `Not working: ${r.error}`, !r.ok); }
        catch (e) { if (e.message !== "login") toast(e.message, true); }
        tb.disabled = false; tb.textContent = "Test"; }); return tb; })(), " ",
      el("button", { class: "btn small", text: "Edit", onclick: () => { editing = s; name.value = s.name; type.value = s.type; url.value = s.url; save.textContent = "Save changes"; hint.textContent = hints[s.type]; name.focus(); } }), " ",
      el("button", { class: "btn small danger", text: "Delete", onclick: async () => { await api("/api/sources/" + s.id, { method: "DELETE" }); route(); } }))));
  shell("sources", [head("Sources", "Where the daily brief gets its evidence."),
    el("div", { class: "panel form" }, el("div", { class: "grid2" }, field("Name", name), field("Type", type)), field("URL or handle", url), hint, el("div", {}, save)),
    el("div", { class: "panel" }, list.length ? el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Name", "Type", "URL or handle", "Active", ""].map((h) => el("th", { text: h })))), el("tbody", {}, rows))) : empty("No sources yet", "Add a few competitors and newsletters you already follow."))]);
}

// ---------- Pictures ----------
async function pagePictures() {
  const pics = await api("/api/pictures");
  const upload = async (files) => {
    for (const f of files) { const fd = new FormData(); fd.append("file", f); try { await api("/api/pictures", { method: "POST", body: fd }); } catch (e) { toast(e.message, true); break; } }
    route();
  };
  const input = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif", multiple: true, hidden: true, onchange: (e) => upload([...e.target.files]) });
  const drop = el("div", { class: "drop", tabindex: "0", role: "button", "aria-label": "Upload images", text: "Drop images here or click to choose (up to 20)" });
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && input.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); upload([...e.dataTransfer.files]); });
  shell("pictures", [head(ST.pictures_label, "Reference photos for thumbnails. Rename this tab in Settings."), drop, input,
    el("div", { class: "pics" }, pics.map((p) => el("div", { class: "pic" }, el("img", { src: "/uploads/" + encodeURIComponent(p.filename), alt: "Uploaded reference" }),
      el("button", { class: "btn small danger", text: "Delete", onclick: async () => { await api("/api/pictures/" + p.id, { method: "DELETE" }); route(); } }))))]);
}

// ---------- Strategy ----------
async function pageStrategy() {
  const c = ST.settings.content;
  const pv = await api("/api/prompt-preview");
  const reels = el("input", { type: "number", min: 0, max: 6, value: c.reels_per_day, "aria-label": "Reels per day" });
  const mix = el("input", { type: "text", value: c.weekly_mix, placeholder: "e.g. 6 reels (1 news, 5 tools) and 2 long videos per week", "aria-label": "Weekly content mix" });
  const cats = el("input", { type: "text", value: c.long_video_categories, "aria-label": "Long video categories" });
  const minEng = el("input", { type: "number", min: 0, step: 100, value: c.min_engagement, "aria-label": "Minimum views or likes" });
  const extra = el("textarea", { value: c.extra_instructions, placeholder: "e.g. Always end reels with a question. Avoid politics." });
  let lv = c.long_video, ar = c.article;
  shell("strategy", [head("Strategy", "What each daily brief contains, and the instructions the AI follows."),
    el("div", { class: "panel form" }, field("Reels per day", reels), field("Weekly content mix (optional, shown to the AI)", mix),
      field("Long video categories (comma separated, alternates day by day)", cats),
      field("Ignore the 'vs. usual' ratio below this many views or likes (stops tiny accounts looking viral, 0 = off)", minEng),
      el("div", { class: "tog" }, "Include one long video", toggle(lv, (v) => (lv = v), "Long video")),
      el("div", { class: "tog" }, "Include one article", toggle(ar, (v) => (ar = v), "Article")),
      field("Extra instructions for the AI", extra),
      el("div", {}, el("button", { class: "btn primary", text: "Save strategy", onclick: async () => { try { const r = await api("/api/settings", { method: "PUT", json: { content: { reels_per_day: reels.value, long_video: lv, article: ar, extra_instructions: extra.value, weekly_mix: mix.value, long_video_categories: cats.value, min_engagement: minEng.value } } }); ST.settings = r.settings; toast("Strategy saved"); route(); } catch (e) { toast(e.message, true); } } }))),
    el("div", { class: "panel" }, el("h2", { text: "Instructions sent to the AI" }), el("p", { class: "muted small", text: "Built from your profile. Edit the profile in Settings and this updates." }), el("pre", { class: "prompt", text: pv.system }))]);
}

// ---------- History ----------
async function pageHistory() {
  const l = await api("/api/briefs");
  shell("history", [head("History", "Every brief that has been generated."),
    el("div", { class: "panel" }, l.length ? el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Date", "Type", "Created", ""].map((h) => el("th", { text: h })))),
      el("tbody", {}, l.map((b) => el("tr", {}, el("td", { text: b.day }), el("td", {}, el("span", { class: "badge " + (b.mode === "ai" ? "ok" : "off"), text: b.mode === "ai" ? "AI" : "Sample" })), el("td", { text: b.created.replace("T", " ").slice(0, 16) }), el("td", {}, el("a", { href: "#/brief/" + b.id, text: "Open" }))))))) : empty("No briefs yet", "Generate your first one from the Daily Brief page."))]);
}

// ---------- Settings ----------
async function pageSettings() {
  const s = ST.settings, p = s.profile;
  const f = {
    creator_name: el("input", { type: "text", value: p.creator_name }), brand_name: el("input", { type: "text", value: p.brand_name }),
    handle: el("input", { type: "text", value: p.handle, placeholder: "@yourhandle" }), niche: el("input", { type: "text", value: p.niche, placeholder: "e.g. AI tools for small business" }),
    language: el("input", { type: "text", value: p.language }), pictures_label: el("input", { type: "text", value: p.pictures_label, placeholder: `Default: ${p.creator_name} Pictures` }),
    tone: el("textarea", { value: p.tone }),
    brand_hashtag: el("input", { type: "text", value: p.brand_hashtag, placeholder: "#yourbrand" }),
    language_rule: el("textarea", { value: p.language_rule, placeholder: "Optional. Example: write in Hinglish (Hindi in English letters)." }),
  };
  const preview = el("div", { class: "muted small" });
  const upd = () => (preview.textContent = `Preview: "${greeting()}, ${f.creator_name.value || "Creator"}"  |  tab: "${f.pictures_label.value || (f.creator_name.value || "Creator") + " Pictures"}"`);
  Object.values(f).forEach((i) => i.addEventListener("input", upd)); upd();
  const plat = { ...s.platforms };
  const budget = el("input", { type: "number", min: 0, max: 500, step: 1, value: s.x_budget, "aria-label": "X monthly budget" });
  const time = el("input", { type: "time", value: s.schedule.time, "aria-label": "Daily run time" });
  const tz = el("input", { type: "text", value: s.schedule.timezone, placeholder: "e.g. Asia/Kolkata (blank = this computer)" });
  let en = s.schedule.enabled, tg = s.notify.telegram;
  const save = async () => {
    try {
      const r = await api("/api/settings", { method: "PUT", json: {
        profile: Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.value])), platforms: plat, x_budget: budget.value,
        schedule: { enabled: en, time: time.value, timezone: tz.value }, notify: { telegram: tg } } });
      ST.settings = r.settings; ST.pictures_label = r.pictures_label; toast("Settings saved"); route();
    } catch (e) { toast(e.message, true); }
  };
  const keyRow = (label, ok, hint) => el("div", { class: "tog" }, el("div", {}, label, el("div", { class: "muted small", text: hint })), el("span", { class: "badge " + (ok ? "ok" : "off"), text: ok ? "connected" : "not set" }));
  shell("settings", [head("Settings", null, el("button", { class: "btn primary", text: "Save settings", onclick: save })),
    el("div", { class: "panel form" }, el("h2", { text: "Profile" }),
      el("p", { class: "muted small", text: "Everything with your name on it comes from here: the greeting, the sidebar, the pictures tab and the AI instructions." }),
      el("div", { class: "grid2" }, field("Your name", f.creator_name), field("Brand or channel name", f.brand_name)),
      el("div", { class: "grid2" }, field("Handle", f.handle), field("Niche", f.niche)),
      el("div", { class: "grid2" }, field("Content language", f.language), field("Pictures tab name", f.pictures_label)),
      el("div", { class: "grid2" }, field("Brand hashtag", f.brand_hashtag), el("div")),
      field("Tone of voice", f.tone),
      field("Language rule for the AI", f.language_rule),
      el("div", {}, el("button", { class: "btn small", type: "button", text: "Use Hindi + English preset", onclick: () => { f.language.value = "Hindi + English"; f.language_rule.value = HINDI_RULE; upd(); toast("Preset filled in. Press Save settings."); } })),
      preview),
    el("div", { class: "panel" }, el("h2", { text: "Platforms" }),
      ...[["youtube", "YouTube"], ["news", "News and newsletters"], ["instagram", "Instagram"], ["facebook", "Facebook"], ["x", "X (optional)"]].map(([k, l]) =>
        el("div", { class: "tog" }, el("div", {}, l, k === "x" ? el("div", { class: "muted small", text: `Off by default. X bills per post read. Spent this month: $${ST.x_spent.toFixed(2)}.` }) : null), toggle(plat[k], (v) => (plat[k] = v), l))),
      el("div", { class: "form", style: "margin-top:12px" }, field("X monthly budget in USD (fetching stops at this limit)", budget))),
    el("div", { class: "panel form" }, el("h2", { text: "Daily run" }), el("div", { class: "tog" }, "Run automatically every day", toggle(en, (v) => (en = v), "Auto run")),
      el("div", { class: "grid2" }, field("Time", time), field("Time zone", tz)), el("p", { class: "muted small", text: "The app must be running at that time. Keep it on a computer or server that stays on." }),
      el("div", { class: "tog" }, "Send a summary to Telegram", toggle(tg, (v) => (tg = v), "Telegram"))),
    el("div", { class: "panel" }, el("h2", { text: "Connections" }), el("p", { class: "muted small", text: "Keys live in the .env file on the server and are never shown here." }),
      keyRow("AI writing (Anthropic)", ST.keys.anthropic, `Model: ${ST.model}`), keyRow("YouTube Data API", ST.keys.youtube, "Optional. Without it, public RSS is used."),
      keyRow("Apify (Instagram and Facebook)", ST.keys.apify, "Needed for Instagram and Facebook sources."),
      el("div", { class: "row", style: "margin:8px 0 6px" }, (() => { const ab = el("button", { class: "btn small", text: "Test Apify token" }); ab.addEventListener("click", async () => {
        ab.disabled = true; ab.textContent = "Testing…";
        try { const r = await api("/api/test/apify", { method: "POST" }); toast(r.ok ? `Apify connected${r.username ? " as " + r.username : ""}` : r.error, !r.ok); }
        catch (e) { if (e.message !== "login") toast(e.message, true); }
        ab.disabled = false; ab.textContent = "Test Apify token"; }); return ab; })()), keyRow("X API", ST.keys.x, "Only needed if X is on."), keyRow("Telegram bot", ST.keys.telegram, "Bot token and chat ID."),
      el("div", { class: "row", style: "margin:8px 0 0" }, (() => { const tb = el("button", { class: "btn small", text: "Send Telegram test" }); tb.addEventListener("click", async () => {
        tb.disabled = true; tb.textContent = "Sending…";
        try { const r = await api("/api/test/telegram", { method: "POST" }); toast(r.ok ? "Test message sent. Check Telegram." : r.error, !r.ok); }
        catch (e) { if (e.message !== "login") toast(e.message, true); }
        tb.disabled = false; tb.textContent = "Send Telegram test"; }); return tb; })())),
    el("div", { class: "panel" }, el("h2", { text: "Sample data" }), el("p", { class: "muted small", text: "Load a few fake sources and posts to explore the app, then remove them." }),
      el("div", { class: "row" }, el("button", { class: "btn", text: "Load sample data", onclick: async () => { await api("/api/demo/seed", { method: "POST" }); toast("Sample data loaded"); } }),
        el("button", { class: "btn danger", text: "Remove sample data", onclick: async () => { await api("/api/demo/clear", { method: "POST" }); toast("Sample data removed"); } })))]);
}

// ---------- router ----------
const PAGES = { brief: pageBrief, hooks: pageHooks, competitors: pageCompetitors, sources: pageSources, pictures: pagePictures, strategy: pageStrategy, history: pageHistory, settings: pageSettings };
async function route() {
  clearInterval(pollTimer);
  const [, name, arg] = (location.hash || "#/brief").split("/");
  try { ST = await api("/api/state"); await (PAGES[name] || pageBrief)(arg); document.title = `${ST.settings.profile.brand_name} · Content HQ`; }
  catch (e) { if (e.message !== "login") { $app.replaceChildren(el("div", { class: "login" }, el("h1", { text: "Something broke" }), el("p", { class: "muted", text: e.message }))); } }
}
async function boot() {
  const s = await fetch("/api/session").then((r) => r.json());
  if (s.auth_required && !s.logged_in) return showLogin();
  route();
}
window.addEventListener("hashchange", route);
boot();
