/* The Library of Babel intro. Picking an article flies down the air shaft of an endless stack of
   hexagonal galleries to the article's own volume, which slides off the shelf and opens in front of
   you; its pages settle into the article's first lines, and the camera falls into the page just as
   the reader appears underneath. It plays over the reader while the download runs, so it never
   delays reading: a slow download just means more pages riffle by.

   app.js calls BabelIntro.play(article, status) and keeps working as before. Three.js is loaded only
   when first needed. Esc, Space, Enter or a click skip; reduced-motion users get it off by default. */

const THREE_SRC = '/static/vendor/three/three.module.min.js';
const PREF_KEY = 'babelIntro';
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');

let THREE = null;
let world = null;     // scene, built once and reused
let active = null;    // the run currently on screen
let loading = null;

/* ------------------------------------------------------------ the library's measurements (metres) */
const RV = 4.2, AP = RV * Math.cos(Math.PI / 6);  // hexagon circumradius, apothem
const H = 3.4, SLAB = 0.22, WALL_T = 0.25;        // floor-to-floor height, slab and wall thickness
const SHAFT = 1.45;                               // air shaft circumradius
const DOOR_W = 1.4, DOOR_H = 2.5, CORR = 1.6;     // the hallway to the next gallery
const PITCH = 2 * (AP + WALL_T) + CORR;           // centre-to-centre distance between galleries
const DOORS = new Set([0, 3]);                    // two free sides, four walls of shelves
const BW = 3.7, BD = 0.36, SHELF0 = 0.1, SHELF_H = 0.44, SHELVES = 5, BOARD = 0.03;
const TARGET = { side: 2, shelf: 3, x: 0.35 };
const BOOK = { t: 0.074, h: 0.34, d: 0.25, ct: 0.006 };  // the chosen volume: spine width, height, depth, board
const LAMP = { y: 2.05, r: SHAFT + 0.1, corners: [1, 4] };  // on two railing posts, facing each other across the shaft
const GOLD = '#d7b15a', GOLD_DARK = '#8a6a2a', INK = '#2c2117', PAPER = '#efe5cf';
const SERIF = '"Sitka Text", Georgia, "Times New Roman", serif';
const GLYPHS = 'abcdefghijlmnopqrstuvxz';  // Borges's twenty-two letters; space, comma and period are added below

/* timeline, seconds */
// each beat starts before the last one ends, so the camera and the book never come to a dead stop
const TL = {
  fly: 2.7, glow: 1.9,          // down the shaft and across to the shelf; the spine warms as you arrive
  slideAt: 2.4, slide: 0.38,    // the volume eases off the shelf while the camera settles
  liftAt: 2.7, lift: 0.65,      // ...and comes to hand
  openAt: 3.2, open: 0.7,       // the board opens as it arrives
  flip: 0.5, idleFlip: 0.9,     // page turns; one every idleFlip while the download is still running
  decode: 0.55, push: 0.8, fade: 0.35,
};

/* ------------------------------------------------------------ small helpers */
const clamp01 = x => Math.max(0, Math.min(1, x));
const lerp = (a, b, t) => a + (b - a) * t;
const ease = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const easeOut = t => 1 - Math.pow(1 - t, 3);
const easeIn = t => t * t * t;
const smooth = (a, b, x) => { const t = clamp01((x - a) / (b - a)); return t * t * (3 - 2 * t); };
const span = (t, start, dur) => clamp01((t - start) / dur);

function rngFrom(seed) {  // mulberry32
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6D2B79F5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hash(str) {
  let h = 2166136261;
  for (const ch of String(str)) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
  return h >>> 0;
}

function pref() { try { return JSON.parse(localStorage.getItem(PREF_KEY)); } catch { return null; } }
function supported() {
  try { return !!document.createElement('canvas').getContext('webgl2'); } catch { return false; }
}
const canRun = supported();
function enabled() { const p = pref(); return canRun && (p === null ? !reduceMotion.matches : !!p); }
function setEnabled(on) { try { localStorage.setItem(PREF_KEY, JSON.stringify(!!on)); } catch {} syncButton(); }

function loadThree() {
  loading ||= import(THREE_SRC).then(m => { THREE = m; }).catch(err => { loading = null; throw err; });
  return loading;
}

/* ------------------------------------------------------------ canvas drawing */
function makeCanvas(w, h) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  return [c, c.getContext('2d')];
}

function grain(g, w, h, rng, n, dark = 0.1, light = 0.06, size = 3) {
  for (let i = 0; i < n; i++) {
    const r = rng() * size + 0.5;
    g.fillStyle = rng() < 0.55 ? `rgba(0,0,0,${dark * rng()})` : `rgba(255,255,255,${light * rng()})`;
    g.beginPath(); g.ellipse(rng() * w, rng() * h, r, r * (0.4 + rng()), rng() * 3, 0, Math.PI * 2); g.fill();
  }
}

function leather(g, w, h, base, rng) {
  g.fillStyle = base; g.fillRect(0, 0, w, h);
  grain(g, w, h, rng, (w * h) / 300, 0.22, 0.025, 2.2);
  const v = g.createRadialGradient(w / 2, h / 2, Math.min(w, h) * 0.2, w / 2, h / 2, Math.max(w, h) * 0.75);
  v.addColorStop(0, 'rgba(255,240,220,0.05)'); v.addColorStop(1, 'rgba(0,0,0,0.35)');
  g.fillStyle = v; g.fillRect(0, 0, w, h);
}

function paper(g, w, h, rng) {
  g.fillStyle = PAPER; g.fillRect(0, 0, w, h);
  grain(g, w, h, rng, 2600, 0.035, 0.05, 2);
  for (let i = 0; i < 7; i++) {  // foxing
    const x = rng() * w, y = rng() * h, r = 6 + rng() * 26;
    const f = g.createRadialGradient(x, y, 0, x, y, r);
    f.addColorStop(0, 'rgba(150,100,40,0.10)'); f.addColorStop(1, 'rgba(150,100,40,0)');
    g.fillStyle = f; g.fillRect(x - r, y - r, r * 2, r * 2);
  }
  const edge = g.createLinearGradient(0, 0, w, 0);  // the gutter darkens toward the spine
  edge.addColorStop(0, 'rgba(90,60,20,0.16)'); edge.addColorStop(0.08, 'rgba(90,60,20,0)');
  edge.addColorStop(0.94, 'rgba(90,60,20,0)'); edge.addColorStop(1, 'rgba(90,60,20,0.08)');
  g.fillStyle = edge; g.fillRect(0, 0, w, h);
}

function gilt(g, fn) {  // gold leaf pressed into leather: a dark lip under a gold fill
  g.save();
  g.shadowColor = 'rgba(0,0,0,0.65)'; g.shadowOffsetY = 2; g.shadowBlur = 2;
  g.fillStyle = GOLD; g.strokeStyle = GOLD;
  fn();
  g.restore();
}

function wrap(g, text, maxW) {
  const words = String(text).split(/\s+/).filter(Boolean);
  const lines = [];
  let line = '';
  for (const w of words) {
    const next = line ? `${line} ${w}` : w;
    if (g.measureText(next).width <= maxW || !line) line = next;
    else { lines.push(line); line = w; }
  }
  if (line) lines.push(line);
  return lines;
}

/** Largest font size (from `max` down) at which `text` fits in `maxLines` lines of `maxW`. */
function fit(g, text, font, maxW, maxLines, max, min) {
  for (let size = max; size >= min; size -= 2) {
    g.font = font(size);
    const lines = wrap(g, text, maxW);
    if (lines.length <= maxLines && lines.every(l => g.measureText(l).width <= maxW)) return { size, lines };
  }
  g.font = font(min);
  let lines = wrap(g, text, maxW);
  if (lines.length > maxLines) {
    lines = lines.slice(0, maxLines);
    let last = lines[maxLines - 1];
    while (last.length > 1 && g.measureText(`${last}…`).width > maxW) last = last.slice(0, -1);
    lines[maxLines - 1] = `${last.trimEnd()}…`;
  }
  return { size: min, lines };
}

function hexPath(g, x, y, r, rot = 0) {
  g.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = rot + (Math.PI / 3) * i;
    g[i ? 'lineTo' : 'moveTo'](x + r * Math.cos(a), y + r * Math.sin(a));
  }
  g.closePath();
}

function babelLine(rng, n) {
  let s = '';
  while (s.length < n) {
    const r = rng();
    s += r < 0.16 ? ' ' : r < 0.175 ? ',' : r < 0.185 ? '.' : GLYPHS[(rng() * GLYPHS.length) | 0];
  }
  return s;
}

/* spine variants for the anonymous books: grey leather grain (tinted per book) and gold tooling */
const SPINE_VARIANTS = 12;
function spineAtlas() {  // all twelve designs side by side, so every book shares one texture and one draw
  const [c, g] = makeCanvas(64 * SPINE_VARIANTS, 256);
  for (let v = 0; v < SPINE_VARIANTS; v++) g.drawImage(spineVariant(v), v * 64, 0);
  return c;
}
function spineVariant(v) {
  const rng = rngFrom(900 + v);
  const [c, g] = makeCanvas(64, 256);
  g.fillStyle = '#d0d0d0'; g.fillRect(0, 0, 64, 256);
  grain(g, 64, 256, rng, 260, 0.2, 0.05, 1.4);
  const style = v % 4;
  const bands = style === 1 ? [[40, 46], [210, 216]] : [[14, 20], [236, 242]];
  if (style === 2) bands.push([70, 74], [182, 186]);
  if (style === 3) bands.push([120, 124]);
  g.fillStyle = GOLD;
  for (const [a, b] of bands) { g.fillRect(5, a, 54, 1.5); g.fillRect(5, b, 54, 1.5); }
  if (v % 3 === 0) {  // a darker title label
    const y = 60 + (rng() * 30) | 0;
    g.fillStyle = 'rgba(0,0,0,0.35)'; g.fillRect(9, y, 46, 60);
    g.fillStyle = GOLD; g.fillRect(9, y, 46, 1); g.fillRect(9, y + 59, 46, 1);
  }
  g.globalAlpha = 0.55;
  g.fillStyle = GOLD;
  g.font = `8px ${SERIF}`;
  g.save(); g.translate(30, 90 + rng() * 20); g.rotate(Math.PI / 2);
  g.fillText(babelLine(rng, 4 + (rng() * 5) | 0).trim(), 0, 3);
  g.restore();
  g.globalAlpha = 1;
  return c;
}

function woodCanvas() {
  const rng = rngFrom(7);
  const [c, g] = makeCanvas(128, 512);
  g.fillStyle = '#7a5a3e'; g.fillRect(0, 0, 128, 512);
  for (let i = 0; i < 90; i++) {
    g.strokeStyle = `rgba(${rng() < 0.5 ? '40,24,12' : '150,110,70'},${0.08 + rng() * 0.18})`;
    g.lineWidth = 0.6 + rng() * 2.2;
    const x = rng() * 128;
    g.beginPath(); g.moveTo(x, 0);
    for (let y = 0; y <= 512; y += 32) g.lineTo(x + Math.sin(y / 60 + i) * 3 + rng() * 1.5, y);
    g.stroke();
  }
  return c;
}

function stoneCanvas() {
  const rng = rngFrom(11);
  const [c, g] = makeCanvas(256, 256);
  g.fillStyle = '#8a7a66'; g.fillRect(0, 0, 256, 256);
  grain(g, 256, 256, rng, 1800, 0.12, 0.08, 2);
  g.strokeStyle = 'rgba(30,20,12,0.55)'; g.lineWidth = 3;
  g.strokeRect(0, 0, 256, 128); g.strokeRect(0, 128, 128, 128); g.strokeRect(128, 128, 128, 128);
  return c;
}

function plasterCanvas() {
  const rng = rngFrom(13);
  const [c, g] = makeCanvas(256, 256);
  g.fillStyle = '#9a8670'; g.fillRect(0, 0, 256, 256);
  grain(g, 256, 256, rng, 1400, 0.08, 0.06, 5);
  return c;
}

function glowCanvas() {
  const [c, g] = makeCanvas(128, 128);
  const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  // the globe hides the middle of the halo, so the glow has to carry well past its rim
  r.addColorStop(0, 'rgba(255,225,170,1)'); r.addColorStop(0.2, 'rgba(255,200,130,0.7)');
  r.addColorStop(0.45, 'rgba(255,170,90,0.25)'); r.addColorStop(1, 'rgba(255,150,70,0)');
  g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  return c;
}

function moteCanvas() {
  const [c, g] = makeCanvas(32, 32);
  const r = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  r.addColorStop(0, 'rgba(255,235,200,1)'); r.addColorStop(1, 'rgba(255,235,200,0)');
  g.fillStyle = r; g.fillRect(0, 0, 32, 32);
  return c;
}

/* ------------------------------------------------------------ the chosen volume's printed surfaces */
const PAGE_W = 1024, PAGE_H = 1400;
const LEATHERS = ['#7a1d17', '#1f4d34', '#1e3566', '#83461d', '#55203f'];  // oxblood, bottle green, navy, tan, plum

function describe(a) {
  const h = hash(a.id || a.url || a.title);
  return {
    seed: h,
    leather: LEATHERS[h % LEATHERS.length],
    place: `Hexagon ${(1000 + (h % 98000)).toLocaleString()} · Wall ${1 + ((h >>> 3) % 4)} · Shelf ${1 + ((h >>> 5) % 5)} · Volume ${1 + ((h >>> 7) % 32)}`,
  };
}

function coverCanvas(a, info) {
  const W = 512, Hc = 696, rng = rngFrom(info.seed);
  const [c, g] = makeCanvas(W, Hc);
  leather(g, W, Hc, info.leather, rng);
  gilt(g, () => {
    g.lineWidth = 4; g.strokeRect(26, 26, W - 52, Hc - 52);
    g.lineWidth = 1.5; g.strokeRect(38, 38, W - 76, Hc - 76);
    for (const [x, y] of [[38, 38], [W - 38, 38], [38, Hc - 38], [W - 38, Hc - 38]]) {
      g.beginPath(); g.moveTo(x, y - 12); g.lineTo(x + 12, y); g.lineTo(x, y + 12); g.lineTo(x - 12, y); g.closePath(); g.fill();
    }
    const { size, lines } = fit(g, a.title || 'Untitled', s => `600 ${s}px ${SERIF}`, W - 130, 6, 48, 22);
    g.font = `600 ${size}px ${SERIF}`; g.textAlign = 'center'; g.textBaseline = 'middle';
    const lh = size * 1.18, top = 250 - ((lines.length - 1) * lh) / 2;
    lines.forEach((l, i) => g.fillText(l, W / 2, top + i * lh));
    const below = top + (lines.length - 1) * lh + size;
    g.fillRect(W / 2 - 50, below + 8, 100, 2);
    if (a.author) {
      const au = fit(g, a.author, s => `italic ${s}px ${SERIF}`, W - 150, 1, 26, 16);
      g.font = `italic ${au.size}px ${SERIF}`;
      g.fillText(au.lines[0], W / 2, below + 44);
    }
    g.lineWidth = 2.5; hexPath(g, W / 2, Hc - 118, 26, Math.PI / 6); g.stroke();
    g.lineWidth = 1.2; hexPath(g, W / 2, Hc - 118, 17, Math.PI / 6); g.stroke();
    g.font = `600 13px ${SERIF}`;
    g.fillText('M E D I U M   L I B R A R Y', W / 2, Hc - 70);
  });
  return c;
}

function backCanvas(info) {
  const W = 256, Hc = 348, rng = rngFrom(info.seed + 1);
  const [c, g] = makeCanvas(W, Hc);
  leather(g, W, Hc, info.leather, rng);
  gilt(g, () => { g.lineWidth = 2; g.strokeRect(13, 13, W - 26, Hc - 26); });
  return c;
}

function spineCanvas(a, info) {
  const W = 256, Hs = 1176, rng = rngFrom(info.seed + 2);
  const [c, g] = makeCanvas(W, Hs);
  leather(g, W, Hs, info.leather, rng);
  const band = y => {  // a raised band across the spine
    const s = g.createLinearGradient(0, y - 12, 0, y + 12);
    s.addColorStop(0, 'rgba(0,0,0,0.45)'); s.addColorStop(0.45, 'rgba(255,230,190,0.18)'); s.addColorStop(1, 'rgba(0,0,0,0.45)');
    g.fillStyle = s; g.fillRect(0, y - 12, W, 24);
    gilt(g, () => { g.fillRect(10, y - 15, W - 20, 3); g.fillRect(10, y + 12, W - 20, 3); });
  };
  [60, 150, 1010, 1100].forEach(band);
  g.fillStyle = 'rgba(0,0,0,0.42)'; g.fillRect(22, 190, W - 44, 780);  // title label
  gilt(g, () => {
    g.lineWidth = 3; g.strokeRect(22, 190, W - 44, 780);
    g.lineWidth = 1.2; g.strokeRect(32, 200, W - 64, 760);
    hexPath(g, W / 2, 105, 16, Math.PI / 6); g.lineWidth = 2.5; g.stroke();
    g.save();
    g.translate(W / 2, 580); g.rotate(Math.PI / 2);  // reads top to bottom, as English spines do
    const { size, lines } = fit(g, a.title || 'Untitled', s => `600 ${s}px ${SERIF}`, 700, 3, 60, 24);
    g.font = `600 ${size}px ${SERIF}`; g.textAlign = 'center'; g.textBaseline = 'middle';
    const lh = size * 1.12;
    lines.forEach((l, i) => g.fillText(l, 0, (i - (lines.length - 1) / 2) * lh));
    g.restore();
    if (a.author) {
      g.save(); g.translate(W / 2, 1055); g.rotate(Math.PI / 2);
      const au = fit(g, a.author, s => `italic ${s}px ${SERIF}`, 64, 1, 26, 12);
      g.font = `italic ${au.size}px ${SERIF}`; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(au.lines[0], 0, 0);
      g.restore();
    }
  });
  return c;
}

function endpaperCanvas(a, info) {
  const rng = rngFrom(info.seed + 3);
  const [c, g] = makeCanvas(PAGE_W, PAGE_H);
  const inks = ['#1c2b4a', '#7d2621', '#b8892c', '#2c4a3c', '#d9c9a3', '#3b2a4a', '#1c2b4a', '#a65a2a'];
  g.fillStyle = '#1c2b4a'; g.fillRect(0, 0, PAGE_W, PAGE_H);
  const band = 150;  // each band is combed into fine bouquets of colour
  for (let y0 = -band; y0 < PAGE_H + band; y0 += band) {
    const ph = rng() * 6;
    for (let k = 0; k < 44; k++) {
      g.strokeStyle = inks[(k + ((y0 / band) | 0)) % inks.length];
      g.globalAlpha = 0.8;
      g.lineWidth = 3.6;
      const off = (k / 44) * band;
      g.beginPath();
      for (let x = 0; x <= PAGE_W; x += 8) {
        const yy = y0 + off + Math.sin(x * 0.045 + ph) * 9 * Math.sin((off / band) * Math.PI) + Math.sin(x * 0.006 + y0) * 14;
        g[x ? 'lineTo' : 'moveTo'](x, yy);
      }
      g.stroke();
    }
  }
  g.globalAlpha = 1;
  const bw = 520, bh = 420, bx = (PAGE_W - bw) / 2, by = (PAGE_H - bh) / 2;  // bookplate
  g.shadowColor = 'rgba(0,0,0,0.35)'; g.shadowBlur = 14; g.shadowOffsetY = 4;
  g.fillStyle = PAPER; g.fillRect(bx, by, bw, bh);
  g.shadowColor = 'transparent';
  g.strokeStyle = INK; g.lineWidth = 3; g.strokeRect(bx + 16, by + 16, bw - 32, bh - 32);
  g.lineWidth = 1; g.strokeRect(bx + 26, by + 26, bw - 52, bh - 52);
  g.fillStyle = INK; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.font = `600 30px ${SERIF}`; g.fillText('E X   L I B R I S', PAGE_W / 2, by + 90);
  hexPath(g, PAGE_W / 2, by + 190, 46, Math.PI / 6); g.lineWidth = 3; g.stroke();
  hexPath(g, PAGE_W / 2, by + 190, 30, Math.PI / 6); g.lineWidth = 1.5; g.stroke();
  g.font = `italic 34px ${SERIF}`; g.fillText('Medium Library', PAGE_W / 2, by + 290);
  const place = fit(g, info.place, s => `${s}px ${SERIF}`, bw - 90, 1, 20, 12);
  g.font = `${place.size}px ${SERIF}`; g.fillStyle = '#6b5a45';
  g.fillText(place.lines[0], PAGE_W / 2, by + 345);
  return c;
}

function titlePageCanvas(a, info, topicName) {
  const rng = rngFrom(info.seed + 4);
  const [c, g] = makeCanvas(PAGE_W, PAGE_H);
  paper(g, PAGE_W, PAGE_H, rng);
  g.fillStyle = INK; g.textAlign = 'center'; g.textBaseline = 'middle';
  const { size, lines } = fit(g, a.title || 'Untitled', s => `600 ${s}px ${SERIF}`, PAGE_W - 240, 5, 74, 34);
  g.font = `600 ${size}px ${SERIF}`;
  const lh = size * 1.2, top = 470 - ((lines.length - 1) * lh) / 2;
  lines.forEach((l, i) => g.fillText(l, PAGE_W / 2, top + i * lh));
  let y = top + (lines.length - 1) * lh + size + 30;
  g.fillRect(PAGE_W / 2 - 70, y, 140, 2);
  g.beginPath(); g.moveTo(PAGE_W / 2, y - 9); g.lineTo(PAGE_W / 2 + 9, y + 1); g.lineTo(PAGE_W / 2, y + 11); g.lineTo(PAGE_W / 2 - 9, y + 1); g.fill();
  y += 80;
  if (a.author) { g.font = `italic 40px ${SERIF}`; g.fillText(`by ${a.author}`, PAGE_W / 2, y); }
  if (topicName) {
    const tp = fit(g, topicName.toUpperCase().split('').join(' '), s => `${s}px ${SERIF}`, PAGE_W - 200, 1, 26, 14);
    g.font = `${tp.size}px ${SERIF}`; g.fillStyle = '#4e3d2b'; g.fillText(tp.lines[0], PAGE_W / 2, y + 80);
  }
  g.fillStyle = INK;
  hexPath(g, PAGE_W / 2, PAGE_H - 250, 26, Math.PI / 6); g.lineWidth = 2; g.strokeStyle = INK; g.stroke();
  g.font = `600 24px ${SERIF}`; g.fillText('T H E   L I B R A R Y   O F   B A B E L', PAGE_W / 2, PAGE_H - 180);
  g.font = `italic 24px ${SERIF}`; g.fillStyle = '#4e3d2b'; g.fillText(info.place, PAGE_W / 2, PAGE_H - 140);
  return c;
}

function versoCanvas(info) {
  const [c, g] = makeCanvas(PAGE_W, PAGE_H);
  paper(g, PAGE_W, PAGE_H, rngFrom(info.seed + 5));
  g.fillStyle = '#5a4a38'; g.textAlign = 'center'; g.font = `italic 22px ${SERIF}`;
  ['Four hundred and ten pages; forty lines to a page,', 'some eighty letters to a line.', '',
    'The Library is unlimited and cyclical.'].forEach((l, i) => g.fillText(l, PAGE_W / 2, PAGE_H - 300 + i * 34));
  return c;
}

const PAGE_FONT = s => `${s}px ${SERIF}`;
function gibberishCanvas(seed) {
  const rng = rngFrom(seed);
  const [c, g] = makeCanvas(PAGE_W, PAGE_H);
  paper(g, PAGE_W, PAGE_H, rng);
  g.fillStyle = INK; g.textBaseline = 'alphabetic';
  g.font = PAGE_FONT(24); g.textAlign = 'center';
  g.fillText(String(1 + ((rng() * 410) | 0)), PAGE_W / 2, 92);
  g.textAlign = 'left'; g.font = PAGE_FONT(27);
  for (let i = 0; i < 29; i++) {
    let s = babelLine(rng, 62);
    while (g.measureText(s).width > PAGE_W - 200) s = s.slice(0, -1);
    g.fillText(s, 100, 170 + i * 40);
  }
  return c;
}

/** The article's first page, laid out once; draw(progress) decodes it out of Babel's noise. */
function articlePage(a, text) {
  const rng = rngFrom(hash(a.id || a.title) + 6);
  const [c, g] = makeCanvas(PAGE_W, PAGE_H);
  const [bg, bgg] = makeCanvas(PAGE_W, PAGE_H);
  paper(bgg, PAGE_W, PAGE_H, rng);
  const M = 100, maxW = PAGE_W - 2 * M;
  const runs = [];  // {font, color, chars:[{ch, x, y, th}]}
  const layout = (str, font, x0, y, color) => {
    g.font = font;
    const chars = [];
    let x = x0;
    for (const ch of str) {
      const w = g.measureText(ch).width;
      chars.push({ ch, x, y, th: rng() });
      x += w;
    }
    runs.push({ font, color, chars });
  };
  const head = a.author || '';
  if (head) { g.font = `22px ${SERIF}`; const w = g.measureText(head.toUpperCase()).width; layout(head.toUpperCase(), `22px ${SERIF}`, (PAGE_W - w) / 2, 92, '#6b5a45'); }
  const t = fit(g, a.title || 'Untitled', s => `700 ${s}px ${SERIF}`, maxW, 3, 56, 34);
  let y = 200;
  for (const l of t.lines) { layout(l, `700 ${t.size}px ${SERIF}`, M, y, INK); y += t.size * 1.18; }
  y += 18;
  const paras = (text || []).length ? text : [a.snippet || ''];
  g.font = PAGE_FONT(30);
  const lh = 45;
  for (const p of paras) {
    const lines = wrap(g, p, maxW);
    for (const l of lines) {
      if (y > PAGE_H - 110) break;
      layout(l, PAGE_FONT(30), M, y, '#1c140c');
      y += lh;
    }
    y += 14;
    if (y > PAGE_H - 110) break;
  }
  const total = runs.reduce((n, r) => n + r.chars.length, 0) || 1;
  let idx = 0;
  for (const r of runs) for (const ch of r.chars) { ch.th = 0.15 + 0.55 * (idx++ / total) + 0.3 * ch.th; }

  const draw = p => {
    g.drawImage(bg, 0, 0);
    for (const r of runs) {
      g.font = r.font; g.fillStyle = r.color;
      for (const ch of r.chars) {
        if (ch.ch === ' ') continue;
        const real = p >= ch.th;
        if (!real && p < ch.th - 0.35) { g.globalAlpha = 0.85; }
        g.fillText(real ? ch.ch : GLYPHS[(Math.random() * GLYPHS.length) | 0], ch.x, ch.y);
        g.globalAlpha = 1;
      }
    }
  };
  draw(0);
  return { canvas: c, draw };
}

/* ------------------------------------------------------------ building the library */
function gildShader(mat, { tint = true } = {}) {  // gold where the texture is gold, leather elsewhere
  mat.onBeforeCompile = sh => {
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <color_fragment>', `
        float gild = 0.0;
        #ifdef USE_MAP
          vec4 gildTex = texture2D( map, vMapUv );
          // gold leaf is bright and warm; tan or oxblood leather is warm too, but dark
          gild = smoothstep( 0.24, 0.42, gildTex.r ) * smoothstep( 0.12, 0.3, gildTex.r - gildTex.b );
        #endif
        #if defined( USE_COLOR )
          diffuseColor.rgb *= mix( vColor, vec3( 1.0 ), ${tint ? 'gild' : '1.0'} );
        #endif`)
      .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\nroughnessFactor = mix( roughnessFactor, 0.42, gild );')
      .replace('#include <metalnessmap_fragment>', '#include <metalnessmap_fragment>\nmetalnessFactor = mix( metalnessFactor, 0.55, gild );')
      .replace('#include <emissivemap_fragment>', '#include <emissivemap_fragment>\ntotalEmissiveRadiance += diffuseColor.rgb * gild * 0.22;');
  };
  mat.customProgramCacheKey = () => `gild-${tint}`;
  return mat;
}

/** The anonymous books share one Lambert material. Each vertex says which face it belongs to (0 spine,
    1 side, 2 top) and each instance which spine design it wears, so all of them draw in one call. */
function shelfBookMaterial(map) {
  const mat = new THREE.MeshLambertMaterial({ map });
  mat.onBeforeCompile = sh => {
    sh.vertexShader = sh.vertexShader
      .replace('#include <common>', '#include <common>\nattribute float face;\nattribute float variant;\nvarying float vFace;')
      .replace('#include <uv_vertex>', `#include <uv_vertex>
        vMapUv.x = ( variant + vMapUv.x ) / ${SPINE_VARIANTS}.0;
        vFace = face;`);
    sh.fragmentShader = sh.fragmentShader
      .replace('#include <common>', '#include <common>\nvarying float vFace;')
      .replace('#include <map_fragment>', `
        vec4 bookTex = texture2D( map, vMapUv );  // sampled outside any branch, so mipmapping stays smooth
        float gild = step( vFace, 0.5 ) * smoothstep( 0.24, 0.42, bookTex.r ) * smoothstep( 0.12, 0.3, bookTex.r - bookTex.b );`)
      .replace('#include <color_fragment>', `
        vec3 bookBase = vFace < 0.5 ? mix( bookTex.rgb * vColor, bookTex.rgb, gild )
          : vFace < 1.5 ? vColor * 0.8
          : vec3( 0.61, 0.51, 0.32 );  // page edges
        diffuseColor.rgb *= bookBase;`)
      .replace('#include <emissivemap_fragment>', '#include <emissivemap_fragment>\ntotalEmissiveRadiance += diffuseColor.rgb * gild * 0.3;');
  };
  mat.customProgramCacheKey = () => 'shelf-book';
  return mat;
}

/** A book as the shelf shows it: spine, two sides and top. The back and bottom are never seen. */
function shelfBookGeometry() {
  const quads = [  // corners counter-clockwise from outside; face id
    [[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1], 0],     // spine (+z)
    [[1, -1, 1], [1, -1, -1], [1, 1, -1], [1, 1, 1], 1],     // +x side
    [[-1, -1, -1], [-1, -1, 1], [-1, 1, 1], [-1, 1, -1], 1], // -x side
    [[-1, 1, 1], [1, 1, 1], [1, 1, -1], [-1, 1, -1], 2],     // top
  ];
  const pos = [], nrm = [], uv = [], face = [], idx = [];
  const normals = [[0, 0, 1], [1, 0, 0], [-1, 0, 0], [0, 1, 0]];
  quads.forEach(([a, b, c, d, f], q) => {
    const base = pos.length / 3;
    for (const v of [a, b, c, d]) { pos.push(v[0] / 2, v[1] / 2, v[2] / 2); nrm.push(...normals[q]); face.push(f); }
    uv.push(0, 0, 1, 0, 1, 1, 0, 1);
    idx.push(base, base + 1, base + 2, base, base + 2, base + 3);
  });
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.Float32BufferAttribute(nrm, 3));
  g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
  g.setAttribute('face', new THREE.Float32BufferAttribute(face, 1));
  g.setIndex(idx);
  return g;
}

function mirrored(mat) {  // the back of a turning leaf: same page texture, read from behind
  mat.onBeforeCompile = sh => {
    sh.fragmentShader = sh.fragmentShader.replace('#include <map_fragment>',
      '#ifdef USE_MAP\n  diffuseColor *= texture2D( map, vec2( 1.0 - vMapUv.x, vMapUv.y ) );\n#endif');
  };
  mat.customProgramCacheKey = () => 'mirrored';
  return mat;
}

function tex(canvas, { repeat, aniso } = {}) {
  const t = new THREE.CanvasTexture(canvas);
  t.colorSpace = THREE.SRGBColorSpace;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(repeat, repeat); }
  if (aniso) t.anisotropy = aniso;
  return t;
}

function sideFrame(side, origin) {
  const phi = (side * Math.PI) / 3;
  const n = new THREE.Vector3(Math.cos(phi), 0, Math.sin(phi));
  const rotY = Math.atan2(-Math.cos(phi), -Math.sin(phi));  // local +z faces into the room
  const m = new THREE.Matrix4().makeRotationY(rotY).setPosition(origin.clone().addScaledVector(n, AP));
  return { n, rotY, m };
}

function buildWorld() {
  const T = THREE;
  const renderer = new T.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  // render above screen resolution for cleaner edges on thin shelves and spines; the frame loop lowers
  // this on GPUs that can't hold the frame rate, and remembers what worked for next time
  const prMax = Math.min(1.5, Math.max(1.25, devicePixelRatio || 1));
  renderer.setPixelRatio(prMax);
  renderer.outputColorSpace = T.SRGBColorSpace;
  renderer.toneMapping = T.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  const aniso = Math.min(8, renderer.capabilities.getMaxAnisotropy());

  const scene = new T.Scene();
  const FOG = new T.Color('#0d0906');
  scene.background = FOG;
  scene.fog = new T.FogExp2(FOG, 0.072);

  const camera = new T.PerspectiveCamera(55, 1, 0.02, 28);  // the fog has swallowed everything past ~25 m

  /* materials */
  const unit = new T.BoxGeometry(1, 1, 1);
  // the architecture is matte, so plain Lambert shading looks the same at a fraction of the cost
  const wood = new T.MeshLambertMaterial({ color: '#6b4a32', map: tex(woodCanvas(), { aniso }) });
  const plaster = new T.MeshLambertMaterial({ color: '#8c7a66', map: tex(plasterCanvas(), { aniso }) });
  const stone = new T.MeshLambertMaterial({ color: '#8a7865', map: tex(stoneCanvas(), { repeat: 0.55, aniso }) });
  const iron = new T.MeshLambertMaterial({ color: '#2a2018' });
  const bulb = new T.MeshBasicMaterial({ color: new T.Color(4.2, 3.0, 1.7) });
  const bookMat = shelfBookMaterial(tex(spineAtlas(), { aniso }));
  const bookGeo = shelfBookGeometry();

  /* instances */
  const B = { wood: [], plaster: [], slab: [], hall: [], iron: [], bulb: [] };

  const lamps = [];
  const mtx = (px, py, pz, sx, sy, sz, ry = 0, rz = 0) => new T.Matrix4().compose(
    new T.Vector3(px, py, pz), new T.Quaternion().setFromEuler(new T.Euler(0, ry, rz)), new T.Vector3(sx, sy, sz));
  const inFrame = (F, local) => new T.Matrix4().multiplyMatrices(F, local);

  // floor ring with the air shaft cut out; its underside is the ceiling of the gallery below
  const outer = (AP + WALL_T) / Math.cos(Math.PI / 6);
  const shape = new T.Shape(), hole = new T.Path();
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 6 + (i * Math.PI) / 3;
    shape[i ? 'lineTo' : 'moveTo'](outer * Math.cos(a), outer * Math.sin(a));
    hole[i ? 'lineTo' : 'moveTo'](SHAFT * Math.cos(a), SHAFT * Math.sin(a));
  }
  shape.holes.push(hole);
  const slabGeo = new T.ExtrudeGeometry(shape, { depth: SLAB, bevelEnabled: false });
  slabGeo.rotateX(Math.PI / 2);

  const leathers = ['#6b1f1a', '#7a3a1e', '#3d2417', '#5a3620', '#2d3b2a', '#1f3b33', '#26334d', '#4a1d2e',
    '#8a6a3a', '#2a2320', '#5c4a2e', '#6d3b1e', '#3a2a4a', '#1e2a2a', '#7b5a2a'].map(c => new T.Color(c));
  const tmpC = new T.Color();

  const rooms = [];
  for (let f = -5; f <= 4; f++) rooms.push({ x: 0, f });
  for (let f = -1; f <= 2; f++) rooms.push({ x: -1, f }, { x: 1, f });
  for (let f = -1; f <= 1; f++) rooms.push({ x: -2, f }, { x: 2, f });
  const has = new Set(rooms.map(r => `${r.x},${r.f}`));

  let targetFrame = null;
  for (const room of rooms) {
    const O = new T.Vector3(room.x * PITCH, room.f * H, 0);
    const rng = rngFrom(hash(`${room.x},${room.f}`));
    const books = { m: [], c: [], v: [] };
    B.slab.push(new T.Matrix4().makeTranslation(O.x, O.y, O.z));
    const wallH = H - SLAB;

    for (let side = 0; side < 6; side++) {
      const F = sideFrame(side, O).m;
      const L = RV + 0.3;
      if (DOORS.has(side)) {
        const jw = (L - DOOR_W) / 2;
        B.plaster.push(inFrame(F, mtx(-(DOOR_W / 2 + jw / 2), wallH / 2, -WALL_T / 2, jw, wallH, WALL_T)));
        B.plaster.push(inFrame(F, mtx(DOOR_W / 2 + jw / 2, wallH / 2, -WALL_T / 2, jw, wallH, WALL_T)));
        B.plaster.push(inFrame(F, mtx(0, (DOOR_H + wallH) / 2, -WALL_T / 2, DOOR_W, wallH - DOOR_H, WALL_T)));
        B.wood.push(inFrame(F, mtx(0, DOOR_H + 0.05, 0.02, DOOR_W + 0.3, 0.1, 0.06)));  // lintel trim
        const dir = side === 0 ? 1 : -1;
        if (side === 0 && has.has(`${room.x + dir},${room.f}`)) {  // the hallway, built once per pair
          const len = 2 * WALL_T + CORR, zc = -len / 2;
          B.hall.push(inFrame(F, mtx(0, -SLAB / 2, zc, DOOR_W + 0.4, SLAB, len)));
          B.plaster.push(inFrame(F, mtx(0, DOOR_H + 0.1, zc, DOOR_W + 0.4, 0.2, len)));
          B.plaster.push(inFrame(F, mtx(-(DOOR_W / 2 + 0.1), DOOR_H / 2, zc, 0.2, DOOR_H, len)));
          B.plaster.push(inFrame(F, mtx(DOOR_W / 2 + 0.1, DOOR_H / 2, zc, 0.2, DOOR_H, len)));
        }
        continue;
      }
      B.plaster.push(inFrame(F, mtx(0, wallH / 2, -WALL_T / 2, L, wallH, WALL_T)));
      const topY = SHELF0 + SHELVES * SHELF_H;
      B.wood.push(inFrame(F, mtx(-BW / 2 + 0.02, topY / 2, BD / 2, 0.04, topY, BD)));
      B.wood.push(inFrame(F, mtx(BW / 2 - 0.02, topY / 2, BD / 2, 0.04, topY, BD)));
      B.wood.push(inFrame(F, mtx(0, topY / 2, 0.01, BW, topY, 0.02)));
      B.wood.push(inFrame(F, mtx(0, (SHELF0 - BOARD) / 2, BD / 2 + 0.01, BW, SHELF0 - BOARD, BD)));
      B.wood.push(inFrame(F, mtx(0, topY + 0.05, BD / 2 + 0.02, BW + 0.1, 0.1, BD + 0.06)));  // cornice
      B.wood.push(inFrame(F, mtx(0, wallH - 0.06, 0.03, L, 0.12, 0.08)));  // crown moulding
      for (let k = 0; k <= SHELVES; k++) B.wood.push(inFrame(F, mtx(0, SHELF0 + k * SHELF_H - BOARD / 2, BD / 2, BW - 0.06, BOARD, BD)));

      const isTarget = room.x === 0 && room.f === 0 && side === TARGET.side;
      if (isTarget) targetFrame = sideFrame(side, O);
      for (let k = 0; k < SHELVES; k++) {
        const base = SHELF0 + k * SHELF_H;
        const reserve = isTarget && k === TARGET.shelf
          ? [TARGET.x - BOOK.t / 2 - 0.006, TARGET.x + BOOK.t / 2 + 0.006] : null;
        let x = -BW / 2 + 0.05, lean = 0;
        const end = BW / 2 - 0.05;
        while (x < end) {
          if (rng() < 0.025) { x += 0.05 + rng() * 0.12; lean = rng() < 0.5 ? 0.14 + rng() * 0.1 : 0; continue; }
          const t = 0.036 + rng() * 0.036, h = 0.27 + rng() * 0.1, d = 0.2 + rng() * 0.06;
          if (x + t > end) break;
          if (reserve && x + t > reserve[0] && x < reserve[1]) { x = reserve[1]; lean = 0; continue; }
          const zs = BD - 0.02 - rng() * 0.025;
          const cx = x + t / 2 + (lean ? (h / 2) * Math.sin(lean) : 0);
          const cy = base + (h / 2) * Math.cos(lean) + (lean ? (t / 2) * Math.sin(lean) : 0);
          books.v.push((rng() * SPINE_VARIANTS) | 0);
          books.m.push(inFrame(F, mtx(cx, cy, zs - d / 2, t, h, d, 0, lean)));
          tmpC.copy(leathers[(rng() * leathers.length) | 0]).offsetHSL((rng() - 0.5) * 0.03, (rng() - 0.5) * 0.1, (rng() - 0.5) * 0.06);
          books.c.push(tmpC.clone());
          x += t + (lean ? h * Math.sin(lean) : 0.0015);
          lean = 0;
        }
      }
    }

    // railing around the air shaft
    const RR = SHAFT + 0.1;
    for (let i = 0; i < 6; i++) {
      const a = Math.PI / 6 + (i * Math.PI) / 3;
      B.wood.push(mtx(O.x + RR * Math.cos(a), O.y + 0.5, O.z + RR * Math.sin(a), 0.07, 1.0, 0.07));
      const phi = Math.PI / 3 + (i * Math.PI) / 3, ap = RR * Math.cos(Math.PI / 6);
      const cx = O.x + ap * Math.cos(phi), cz = O.z + ap * Math.sin(phi), ry = -(phi + Math.PI / 2);
      B.wood.push(mtx(cx, O.y + 1.0, cz, RR + 0.06, 0.07, 0.09, ry));
      B.wood.push(mtx(cx, O.y + 0.45, cz, RR, 0.035, 0.035, ry));
      B.wood.push(mtx(cx, O.y + 0.5, cz, 0.04, 1.0, 0.04, ry));
    }
    // two lamps, set transversally: glass globes on tall posts at opposite corners of the railing
    for (const i of LAMP.corners) {
      const ang = Math.PI / 6 + (i * Math.PI) / 3;
      const p = new T.Vector3(O.x + LAMP.r * Math.cos(ang), O.y + LAMP.y, O.z + LAMP.r * Math.sin(ang));
      lamps.push(p);
      B.bulb.push(mtx(p.x, p.y, p.z, 1, 1, 1));
      B.iron.push(mtx(p.x, O.y + (LAMP.y - 0.1) / 2 + 0.5, p.z, 0.035, LAMP.y - 1.1, 0.035));
      B.iron.push(mtx(p.x, p.y - 0.13, p.z, 0.12, 0.03, 0.12));
    }

    const geo = bookGeo.clone();
    geo.setAttribute('variant', new T.InstancedBufferAttribute(new Float32Array(books.v), 1));
    const shelf = new T.InstancedMesh(geo, bookMat, books.m.length);
    books.m.forEach((m, i) => { shelf.setMatrixAt(i, m); shelf.setColorAt(i, books.c[i]); });
    shelf.matrixAutoUpdate = false;
    shelf.computeBoundingSphere();  // per gallery, so the frustum test can drop it
    scene.add(shelf);
  }

  const statics = [
    [unit, wood, B.wood], [unit, plaster, B.plaster], [slabGeo, stone, B.slab], [unit, stone, B.hall],
    [unit, iron, B.iron], [new T.SphereGeometry(0.12, 20, 14), bulb, B.bulb],
  ];
  for (const [geo, mat, list] of statics) {
    if (!list.length) continue;
    const mesh = new T.InstancedMesh(geo, mat, list.length);
    list.forEach((m, i) => mesh.setMatrixAt(i, m));
    mesh.matrixAutoUpdate = false;
    scene.add(mesh);
  }

  // halos around every lamp, and dust in the light
  const haloGeo = new T.BufferGeometry().setFromPoints(lamps);
  scene.add(new T.Points(haloGeo, new T.PointsMaterial({
    size: 2.4, map: tex(glowCanvas()), transparent: true, depthWrite: false, blending: T.AdditiveBlending, color: '#ffc98a',
  })));
  const rngD = rngFrom(5), dustPts = [];
  for (let i = 0; i < 700; i++) {
    const a = rngD() * Math.PI * 2, rr = Math.sqrt(rngD()) * (AP - 0.3);
    dustPts.push(new T.Vector3(Math.cos(a) * rr, rngD() * 7 - 1, Math.sin(a) * rr));
  }
  const dust = new T.Points(new T.BufferGeometry().setFromPoints(dustPts), new T.PointsMaterial({
    size: 0.018, map: tex(moteCanvas()), transparent: true, opacity: 0.55, depthWrite: false, blending: T.AdditiveBlending, color: '#ffdcb0',
  }));
  scene.add(dust);

  // light: the destination gallery's two lamps, a dim warm fill everywhere, and a reading light that follows the camera
  scene.add(new T.HemisphereLight('#ffdcb0', '#2a1c12', 1.25));
  for (const i of LAMP.corners) {  // the destination gallery's lamps
    const ang = Math.PI / 6 + (i * Math.PI) / 3;
    const l = new T.PointLight('#ffc98a', 11, 12, 2);
    l.position.set(LAMP.r * Math.cos(ang), LAMP.y, LAMP.r * Math.sin(ang));
    scene.add(l);
  }
  // one light rides with the camera: it lights the galleries it falls past, then dims to a reading light
  const readingLight = new T.PointLight('#ffd6a0', 0, 9, 2);
  scene.add(readingLight);

  return { renderer, camera, scene, aniso, targetFrame, dust, readingLight, pr: prMax, prMax, prMin: 0.75 };
}

/* ------------------------------------------------------------ the chosen volume */
function buildVolume(a, topicName) {
  const T = THREE, { t, h, d, ct } = BOOK;
  const info = describe(a);
  const aniso = world.aniso;
  const disposables = [];
  const tx = (canvas, opts) => { const x = tex(canvas, { aniso, ...opts }); disposables.push(x); return x; };
  const mat = (opts, gild = false) => {
    const m = new T.MeshStandardMaterial({ roughness: 0.7, ...opts });
    disposables.push(m);
    return gild ? gildShader(m, { tint: false }) : m;
  };
  const leatherCol = new T.Color(info.leather);
  const plain = mat({ color: leatherCol.clone().multiplyScalar(1.1), roughness: 0.78 });
  const edges = mat({ color: '#d8c9a6', roughness: 0.95 });
  const spineMat = mat({ map: tx(spineCanvas(a, info)), emissive: new T.Color('#000000') }, true);
  const coverMat = mat({ map: tx(coverCanvas(a, info)) }, true);
  const backMat = mat({ map: tx(backCanvas(info)) }, true);

  const root = new T.Group();   // pivot the camera frames
  const body = new T.Group();   // slides so the open spread stays centred
  root.add(body);
  const box = (sx, sy, sz, mats, x, y, z, parent = body) => {
    const g = new T.BoxGeometry(sx, sy, sz); disposables.push(g);
    const m = new T.Mesh(g, mats); m.position.set(x, y, z); parent.add(m); return m;
  };
  const zh = d / 2 - ct;                        // spine-side edge of the text block
  const zf = -d / 2 - 0.004;                    // fore-edge of the boards
  box(ct, h, zh - zf, [plain, backMat, plain, plain, plain, plain], -t / 2 + ct / 2, 0, (zh + zf) / 2);  // back board
  box(t, h, ct, [plain, plain, plain, plain, spineMat, plain], 0, 0, d / 2 - ct / 2);                       // spine
  box(t - 2 * ct, h - 0.012, zh - (-d / 2 + 0.002), edges, 0, 0, (zh + (-d / 2 + 0.002)) / 2);              // text block

  const hinge = new T.Group();                  // the front board opens about its inner spine edge
  hinge.position.set(t / 2 - ct, 0, zh);
  body.add(hinge);
  const coverLen = zh - zf;
  box(ct, h, coverLen, [coverMat, plain, plain, plain, plain, plain], ct / 2, 0, -coverLen / 2, hinge);

  const pw = zh - (-d / 2 + 0.004), ph = h - 0.014;
  const planeGeo = new T.PlaneGeometry(pw, ph); disposables.push(planeGeo);
  const pageMat = map => mat({ map, roughness: 0.92 });
  const endpaperTex = tx(endpaperCanvas(a, info));
  const left = new T.Mesh(planeGeo, pageMat(endpaperTex));  // inside the front board
  left.rotation.y = -Math.PI / 2;
  left.position.set(-0.0004, 0, -pw / 2 - 0.002);
  hinge.add(left);
  const titleTex = tx(titlePageCanvas(a, info, topicName));
  const right = new T.Mesh(planeGeo, pageMat(titleTex));    // top of the text block
  right.rotation.y = Math.PI / 2;
  right.position.set(t / 2 - ct + 0.0004, 0, zh - pw / 2);
  body.add(right);

  // leaves that turn: a bendable strip, front and back sharing one geometry
  const leaves = [0, 1, 2].map(i => {
    const geo = new T.PlaneGeometry(pw, ph, 14, 1); disposables.push(geo);
    // start with real maps so the shaders compiled up front are the ones the page turns use
    const front = new T.Mesh(geo, pageMat(titleTex)), back = new T.Mesh(geo, mirrored(pageMat(world.verso)));
    back.material.side = T.BackSide;
    const g = new T.Group();
    g.position.set(t / 2 - ct + 0.0009 + i * 0.0003, 0, zh);
    g.add(front, back); g.visible = false;
    body.add(g);
    const base = geo.attributes.position.array.slice();
    return { g, geo, front, back, base, busy: false };
  });

  return { root, body, hinge, left, right, leaves, spineMat, info, disposables, pw, ph, titleTex, tx };
}

/** Bend a leaf to angle α (0 = lying on the right, π = on the left). The fore-edge leads as the page
    lifts and trails as it falls, so the sheet curls like paper instead of swinging like a door. */
function bendLeaf(leaf, alpha) {
  const pos = leaf.geo.attributes.position, arr = pos.array, base = leaf.base;
  const segs = 14, w = leaf.geo.parameters.width, ds = w / segs;
  const curl = 1.1 * Math.sin(alpha) * (1 - (2 * alpha) / Math.PI);
  const xs = [0], zs = [0];
  for (let i = 1; i <= segs; i++) {
    const s = (i - 0.5) / segs;
    const ang = Math.max(0, Math.min(Math.PI, alpha + curl * s * s));
    xs.push(xs[i - 1] + Math.sin(ang) * ds);   // away from the page block
    zs.push(zs[i - 1] - Math.cos(ang) * ds);   // toward the fore-edge
  }
  for (let v = 0; v < pos.count; v++) {
    const i = Math.round(((base[v * 3] + w / 2) / w) * segs);
    arr[v * 3] = xs[i];
    arr[v * 3 + 1] = base[v * 3 + 1];
    arr[v * 3 + 2] = zs[i];
  }
  pos.needsUpdate = true;
  leaf.geo.computeVertexNormals();
}

/* ------------------------------------------------------------ overlay */
let overlay = null;
function ensureOverlay() {
  if (overlay) return overlay;
  const el = document.createElement('div');
  el.className = 'babel';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = '<div class="babel-veil"></div><div class="babel-place"></div><div class="babel-status"></div><div class="babel-skip">Esc to skip</div>';
  document.body.append(el);
  overlay = { el, place: el.querySelector('.babel-place'), status: el.querySelector('.babel-status') };
  el.addEventListener('pointerdown', () => active?.skip());
  return overlay;
}

function resize() {
  if (!world) return;
  const w = innerWidth, h = innerHeight;
  world.renderer.setPixelRatio(world.pr);
  world.renderer.setSize(w, h, false);
  world.camera.aspect = w / h;
  world.camera.updateProjectionMatrix();
}

/* ------------------------------------------------------------ playback */
/** Load Three.js, build the library and push every shader and texture to the GPU, so nothing compiles
    or uploads mid-flight. Runs on idle after page load, and again (instantly) before each play. */
function prepare() {
  prepare.p ||= loadThree().then(async () => {
    const W = world = buildWorld();
    W.renderer.domElement.className = 'babel-canvas';
    W.gibs = [0, 1, 2, 3].map(i => tex(gibberishCanvas(4000 + i), { aniso: W.aniso }));
    W.verso = tex(versoCanvas({ seed: 99 }), { aniso: W.aniso });
    addEventListener('resize', resize);
    await warm(W.scene);
    for (const t of [...W.gibs, W.verso]) W.renderer.initTexture(t);
  }).catch(err => { prepare.p = null; world = null; throw err; });
  return prepare.p;
}

async function warm(root) {
  const R = world.renderer;
  const hidden = [];
  root.traverse(o => { if (!o.visible) { hidden.push(o); o.visible = true; } });  // compile what's hidden too
  try { await R.compileAsync(root, world.camera, world.scene); } catch { R.compile(root, world.camera, world.scene); }
  hidden.forEach(o => { o.visible = false; });
  root.traverse(o => {
    for (const m of [].concat(o.material || [])) {
      if (m.map && !m.map.userData.sent) { R.initTexture(m.map); m.map.userData.sent = true; }
    }
  });
}

async function play(article, status, opts = {}) {
  if (!enabled() || !article) return;
  if (active) active.stop(true);
  const ov = ensureOverlay();
  ov.el.classList.remove('out');
  ov.el.classList.add('on');
  ov.place.textContent = ''; ov.status.textContent = '';
  const token = {};
  active = { token, stop() { ov.el.classList.remove('on'); active = null; }, skip() { this.stop(); } };
  try {
    await prepare();
    if (active?.token !== token) return;
    ov.el.prepend(world.renderer.domElement);
    resize();
    const vol = buildVolume(article, opts.topicName || '');
    world.scene.add(vol.root);
    await warm(vol.root);
    if (active?.token !== token) { world.scene.remove(vol.root); vol.disposables.forEach(x => x.dispose()); return; }
    start(article, status, token, vol);
  } catch (err) {
    console.warn('Library of Babel intro unavailable', err);
    if (active?.token === token) active.stop();
  }
}

function start(a, status, token, vol) {
  const T = THREE, W = world, cam = W.camera, ov = overlay;

  /* where the volume stands, and the path to it */
  const F = W.targetFrame;
  const shelfQ = new T.Quaternion().setFromRotationMatrix(new T.Matrix4().extractRotation(F.m));
  const local = new T.Vector3(TARGET.x, SHELF0 + TARGET.shelf * SHELF_H + BOOK.h / 2 + 0.001, BD - 0.022 - BOOK.d / 2);
  const P = local.clone().applyMatrix4(F.m);
  const inward = F.n.clone().negate();
  const C2 = P.clone().addScaledVector(inward, 0.92); C2.y += 0.03;
  const rng = rngFrom(vol.info.seed);
  const yaw = rng() * Math.PI * 2;
  const C0 = new T.Vector3(0, 2 * H + 2.0, 0);  // two galleries up: far enough to feel the drop
  const curve = new T.CatmullRomCurve3([
    C0, new T.Vector3(0.15, H + 1.9, -0.1),
    new T.Vector3(C2.x * 0.3, 1.95, C2.z * 0.3), C2,
  ], false, 'centripetal');
  curve.arcLengthDivisions = 2000;
  const d0 = new T.Vector3(Math.sin(yaw) * 0.28, -1, Math.cos(yaw) * 0.28).normalize();
  const d1 = P.clone().sub(C2).normalize();
  const rollDir = rng() < 0.5 ? 1 : -1;

  /* where it is held: in front of the camera, face on, leaning back a little */
  const holdQ = shelfQ.clone()
    .multiply(new T.Quaternion().setFromAxisAngle(new T.Vector3(0, 1, 0), -Math.PI / 2))
    .multiply(new T.Quaternion().setFromAxisAngle(new T.Vector3(0, 0, 1), 0.3));
  // the book is held at arm's length; on narrow screens the camera steps back instead (pushing the book
  // away would bury it in the shelf), and portrait screens frame only the right-hand page
  const HOLD = 0.6;
  const narrow = () => cam.aspect < 0.9;
  const stepBack = () => {
    const vh = 2 * Math.tan(T.MathUtils.degToRad(42 / 2));
    return Math.max(HOLD, (narrow() ? 0.3 : 0.62) / (vh * cam.aspect), 0.42 / vh) - HOLD;
  };
  const slid = P.clone().addScaledVector(inward, 0.2);

  vol.root.position.copy(P);
  vol.root.quaternion.copy(shelfQ);

  ov.place.textContent = vol.info.place;

  /* page turning */
  const pagesState = { rightTex: vol.titleTex, gibIndex: 0, ready: null, article: null, flips: [], nextFlip: Infinity, articleShownAt: Infinity };
  const turn = (t0, nextRight, back) => {  // swapping one texture for another needs no recompile
    const leaf = vol.leaves.find(l => !l.busy);
    if (!leaf) return false;
    leaf.busy = true;
    leaf.front.material.map = pagesState.rightTex;
    leaf.back.material.map = back;
    vol.right.material.map = nextRight;
    pagesState.rightTex = nextRight;
    leaf.g.visible = true;
    bendLeaf(leaf, 0);
    pagesState.flips.push({ leaf, t0, back });
    return true;
  };
  const nextGib = () => W.gibs[pagesState.gibIndex++ % W.gibs.length];

  let last = { state: 'loading' }, lastPoll = -1, t0 = performance.now(), prevT = 0;
  let pushAt = Infinity, endAt = Infinity, stopped = false, articleTex = null, articleDraw = null;
  const openAt = TL.openAt, firstFlipAt = openAt + TL.open - 0.12;
  pagesState.nextFlip = firstFlipAt;

  let preparing = false;
  const readyWith = () => {
    if (pagesState.article || preparing) return;
    preparing = true;
    (window.requestIdleCallback || setTimeout)(buildArticle, { timeout: 250 });
  };
  const buildArticle = () => {
    if (stopped) return;
    const text = [];
    const doc = last.doc;
    if (doc) {
      const sub = doc.querySelector('.subtitle')?.textContent.trim();
      if (sub) text.push(sub);
      for (const p of doc.querySelectorAll('p')) {
        if (p.closest('header, figure, blockquote, pre')) continue;
        const s = p.textContent.replace(/\s+/g, ' ').trim();
        if (s.length > 1) text.push(s);
        if (text.join(' ').length > 1400) break;
      }
    }
    const page = articlePage(a, text);
    articleTex = vol.tx(page.canvas);
    W.renderer.initTexture(articleTex);
    articleDraw = page.draw;
    pagesState.article = articleTex;
  };

  const cleanup = () => {
    if (stopped) return;
    stopped = true;
    // a run that never dropped a frame earns a sharper start next time
    if (pace.frames > 60 && pace.sum / pace.frames < 1 / 55 && W.pr < W.prMax) W.pr = Math.min(W.prMax, +(W.pr * 1.15).toFixed(2));
    W.scene.remove(vol.root);
    vol.disposables.forEach(x => x.dispose());
    W.readingLight.intensity = 0;
    removeEventListener('keydown', onKey, true);
  };
  let finishing = false;
  const finish = (fast = false) => {
    if (active?.token !== token || finishing) return;
    finishing = true;
    ov.el.classList.add('out');
    ov.el.style.setProperty('--babel-fade', `${fast ? 0.25 : TL.fade}s`);
    setTimeout(() => {
      if (active?.token === token) { ov.el.classList.remove('on', 'out'); active = null; }
      cleanup();
    }, (fast ? 0.25 : TL.fade) * 1000 + 30);
  };
  const onKey = e => {
    if (!['Escape', ' ', 'Enter'].includes(e.key)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    finish(true);
  };
  addEventListener('keydown', onKey, true);
  active = {
    token,
    skip: () => finish(true),
    stop: (instant) => { if (instant) { ov.el.classList.remove('on', 'out'); active = null; } cleanup(); },
  };

  const look = new T.Vector3(), dir = new T.Vector3(), tmpV = new T.Vector3(), aim = new T.Matrix4(), LIGHT_OFFSET = new T.Vector3(0, 0.25, 0);
  const hold = C2.clone().addScaledVector(d1, HOLD).add(new T.Vector3(0, -0.03, 0));
  let pushFrom = null;

  // judged on the last 24 frames, so one long frame (the reader rendering underneath) doesn't count,
  // but a GPU that is steadily behind does
  const pace = { last: 0, gaps: [], frames: 0, changed: 0, sum: 0 };
  const adapt = () => {
    const wall = performance.now() / 1000;
    const gap = pace.last ? wall - pace.last : 0;
    pace.last = wall;
    if (!gap || gap > 0.25) return;  // first frame, or the tab was in the background
    pace.frames++; pace.sum += gap;
    pace.gaps.push(gap);
    if (pace.gaps.length > 24) pace.gaps.shift();
    const slow = pace.gaps.filter(g => g > 1 / 45).length;
    if (pace.gaps.length === 24 && slow >= 14 && W.pr > W.prMin && wall - pace.changed > 0.5) {
      W.pr = Math.max(W.prMin, +(W.pr * 0.8).toFixed(2));
      pace.changed = wall; pace.gaps.length = 0;
      resize();
    }
  };

  const frame = now => {
    if (stopped || active?.token !== token) { cleanup(); return; }
    adapt();
    const t = (now - t0) / 1000, dt = Math.min(0.05, t - prevT);
    prevT = t;

    // what the reader underneath is doing
    if (t - lastPoll > 0.15) {
      lastPoll = t;
      try { last = status() || last; } catch { /* keep the last answer */ }
      if (last.state === 'gone') { ov.el.classList.remove('on'); active = null; cleanup(); return; }
      if (last.state === 'error') { finish(true); }
      if (last.state === 'ready') readyWith();
      const line = last.state === 'loading' && t > openAt ? (last.text || '') : '';
      if (ov.status.textContent !== line) ov.status.textContent = line;
    }
    const showPlace = t > 1.2 && t < openAt + 0.6;
    if (ov.place.classList.contains('show') !== showPlace) ov.place.classList.toggle('show', showPlace);

    /* camera: down the shaft, then across to the shelf */
    let fov = cam.fov;
    if (t < TL.fly) {
      fov = lerp(58, 55, t / TL.fly);
      const u = ease(t / TL.fly);
      cam.position.copy(curve.getPointAt(u));
      const w = smooth(0.3, 0.92, u);
      dir.lerpVectors(d0, d1, w).normalize();
      cam.up.set(0, 1, 0);
      cam.lookAt(look.copy(cam.position).add(dir));
      cam.rotateZ(rollDir * 0.5 * (1 - smooth(0, 0.75, u)));
    } else if (!pushFrom) {
      const k = ease(span(t, TL.liftAt, TL.lift));
      cam.position.copy(C2).addScaledVector(d1, -stepBack() * k);
      look.lerpVectors(P, vol.root.position, k);
      cam.up.set(0, 1, 0);
      cam.lookAt(look);
      fov = lerp(55, 42, k);
    }

    /* the volume: glows, slides out, comes to hand */
    const glow = smooth(TL.glow - 0.5, TL.slideAt, t) * (1 - smooth(TL.liftAt, TL.openAt, t));
    const pulse = 0.75 + 0.25 * Math.sin(t * 7);
    vol.spineMat.emissive.setRGB(0.05 * glow * pulse, 0.03 * glow * pulse, 0.008 * glow * pulse);
    const sl = ease(span(t, TL.slideAt, TL.slide));
    const lf = ease(span(t, TL.liftAt, TL.lift));
    if (t < TL.liftAt) {
      const nudge = 0.025 * smooth(TL.glow - 0.3, TL.glow + 0.3, t);  // it edges forward as you arrive
      vol.root.position.lerpVectors(P, slid, sl).addScaledVector(inward, nudge * (1 - sl));
      vol.root.quaternion.copy(shelfQ);
    } else {
      const from = sl < 1 ? tmpV.lerpVectors(P, slid, sl) : slid;  // a lift that starts mid-slide carries on from there
      vol.root.position.lerpVectors(from, hold, lf);
      vol.root.position.y += Math.sin(Math.PI * lf) * 0.05;
      vol.root.quaternion.slerpQuaternions(shelfQ, holdQ, lf);
      if (lf >= 1) vol.root.position.y += Math.sin(t * 1.3) * 0.002;
    }
    // bright while falling past the galleries, easing to a soft reading light as the book arrives
    const fall = 1 - smooth(TL.fly * 0.55, TL.fly, t);
    W.readingLight.position.copy(cam.position).add(LIGHT_OFFSET);
    W.readingLight.intensity = lerp(1.1 * smooth(TL.slideAt, openAt, t), 8, fall);
    W.readingLight.distance = lerp(3, 9, fall);

    /* the front board opens */
    const op = ease(span(t, openAt, TL.open));
    vol.hinge.rotation.y = -Math.PI * op;
    vol.body.position.z = -(narrow() ? BOOK.d / 2 - BOOK.ct - vol.pw / 2 : BOOK.d / 2 - BOOK.ct) * op;

    /* leaves turn: Babel's pages until the download lands, then the article */
    if (t >= pagesState.nextFlip && pushAt === Infinity) {
      if (pagesState.rightTex === vol.titleTex) {
        const next = pagesState.article || nextGib();
        if (turn(t, next, W.verso)) pagesState.nextFlip = pagesState.article ? Infinity : t + TL.idleFlip;
        if (next === pagesState.article) pagesState.articleShownAt = t;
      } else if (pagesState.article) {
        const back = nextGib();
        if (turn(t, pagesState.article, back)) { pagesState.articleShownAt = t; pagesState.nextFlip = Infinity; }
      } else {
        const back = nextGib(), next = nextGib();
        if (turn(t, next, back)) pagesState.nextFlip = t + TL.idleFlip;
      }
    }
    if (pagesState.article && pagesState.nextFlip !== Infinity && pagesState.articleShownAt === Infinity
      && pagesState.nextFlip > t + 0.15 && t > firstFlipAt) pagesState.nextFlip = t + 0.15;  // ready: turn to it soon
    for (const f of pagesState.flips.slice()) {
      const k = span(t, f.t0, TL.flip);
      bendLeaf(f.leaf, Math.PI * ease(k));
      if (k >= 1) {
        f.leaf.g.visible = false; f.leaf.busy = false;
        vol.left.material.map = f.back;
        pagesState.flips.splice(pagesState.flips.indexOf(f), 1);
      }
    }
    if (articleDraw && t >= pagesState.articleShownAt) {
      const p = span(t, pagesState.articleShownAt + 0.1, TL.decode);
      // each redraw re-uploads a whole page, so the letters settle at 20 Hz rather than every frame
      if ((p < 1 && t - (articleTex.userData.drawn ?? -1) >= 0.05) || (p >= 1 && !articleTex.userData.done)) {
        articleDraw(p < 1 ? p : 1.01);
        articleTex.needsUpdate = true;
        articleTex.userData.drawn = t;
        if (p >= 1) articleTex.userData.done = true;
      }
      if (pushAt === Infinity) pushAt = pagesState.articleShownAt + TL.flip * 0.85;  // start moving as the leaf lands
    }

    /* fall into the page */
    if (t >= pushAt) {
      if (!pushFrom) {
        pushFrom = { pos: cam.position.clone(), q: cam.quaternion.clone(), fov: cam.fov };
        vol.root.updateMatrixWorld(true);
        const target = new T.Vector3(BOOK.t / 2 - BOOK.ct, 0.09, BOOK.d / 2 - BOOK.ct - vol.pw * 0.5)
          .applyMatrix4(vol.body.matrixWorld);
        const normal = new T.Vector3(1, 0, 0).applyQuaternion(vol.root.quaternion).normalize();
        const up = new T.Vector3(0, 1, 0).applyQuaternion(vol.root.quaternion).normalize();
        // close enough that paper fills the screen, so the fade lands on the reader's own page
        const dist = Math.min(0.3, (0.8 * vol.pw) / (2 * Math.tan(T.MathUtils.degToRad(38 / 2)) * cam.aspect));
        pushFrom.toPos = target.clone().addScaledVector(normal, dist);
        pushFrom.toQ = new T.Quaternion().setFromRotationMatrix(aim.lookAt(pushFrom.toPos, target, up));
        endAt = pushAt + TL.push;
      }
      const k = ease(span(t, pushAt, TL.push));
      cam.position.lerpVectors(pushFrom.pos, pushFrom.toPos, k);
      cam.quaternion.slerpQuaternions(pushFrom.q, pushFrom.toQ, ease(span(t, pushAt, TL.push * 0.8)));
      fov = lerp(pushFrom.fov, 38, k);
      if (t >= endAt - 0.25) finish();  // the paper fills the screen by now; fade onto the reader
    }

    if (Math.abs(cam.fov - fov) > 0.01) { cam.fov = fov; cam.updateProjectionMatrix(); }
    W.dust.rotation.y += dt * 0.02;
    W.dust.position.y = Math.sin(t * 0.3) * 0.08;
    W.renderer.render(W.scene, cam);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(now => { t0 = now; frame(now); });
}

/* ------------------------------------------------------------ the on/off switch in the sidebar */
function syncButton() {
  const btn = document.getElementById('babelBtn');
  if (!btn) return;
  btn.hidden = false;
  btn.disabled = !canRun;
  const on = enabled();
  btn.setAttribute('aria-pressed', String(on));
  btn.title = !canRun ? 'Library of Babel intro needs WebGL 2 — turn on hardware acceleration in your browser settings'
    : on ? 'Library of Babel intro is on — click to turn it off'
    : pref() === null && reduceMotion.matches ? 'Library of Babel intro is off because your system asks for reduced motion — click to turn it on'
    : 'Library of Babel intro is off — click to turn it on';
}
document.getElementById('babelBtn')?.addEventListener('click', () => setEnabled(!enabled()));
syncButton();

// fetch Three.js while the browser is idle so the first click doesn't wait for it
// build the library while the browser is idle so the first click starts at once
if (enabled()) (window.requestIdleCallback || setTimeout)(() => prepare().catch(() => {}), { timeout: 4000 });

window.BabelIntro = { play, enabled, setEnabled, get playing() { return !!active; } };
