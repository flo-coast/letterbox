// placement.js: KWin script loaded by the letterbox script while GW2 runs in letterbox mode.
// Pins the GW2 window (windowed, no border) to the bottom/center/top of the monitor at full width and
// GAME_H device pixels height, and the stats bar into the (larger, or the top) free strip.
// Geometry is in logical coordinates; dividing by the output scale keeps both windows pixel-exact.
const OUTPUT = "@OUTPUT@";
const GAME_H = @GAME_H@;
const POS = "@POS@";                         // bottom | center | top
const OUT_W = @OUT_W@, OUT_H = @OUT_H@;     // output mode in device pixels (logical sizes get rounded)

function output() {
    const outs = workspace.screens;
    for (let i = 0; i < outs.length; i++)
        if (outs[i].name === OUTPUT) return outs[i];
    return null;
}

function isGame(w) {
    return w.resourceClass === "steam_app_1284210" || /^Guild Wars 2$/.test(w.caption);
}

function isBar(w) {
    return w.resourceClass === "letterbox-bar" || w.resourceClass === "letterbox.bar";
}

function target(w) {
    const o = output();
    if (!o) return null;
    const s = o.devicePixelRatio, g = o.geometry;
    const width = OUT_W / s, free = (OUT_H - GAME_H) / s;
    const gameY = POS === "top" ? 0 : POS === "center" ? Math.round((OUT_H - GAME_H) / 2) / s : free;
    if (isGame(w)) return {x: g.x, y: g.y + gameY, width: width, height: GAME_H / s};
    if (POS === "top") return {x: g.x, y: g.y + GAME_H / s, width: width, height: free};
    return {x: g.x, y: g.y, width: width, height: gameY};   // bottom: whole strip, center: top half
}

function same(a, b) {
    return Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 &&
           Math.abs(a.width - b.width) < 0.5 && Math.abs(a.height - b.height) < 0.5;
}

function place(w) {
    const t = target(w);
    if (!t) return;
    if (w.fullScreen) w.fullScreen = false;
    w.noBorder = true;
    // only the bar stays on top: with the game "above" too, Alt+Tab focused other windows behind it
    w.keepAbove = isBar(w);
    if (!same(w.frameGeometry, t)) w.frameGeometry = t;
}

function manage(w) {
    if (!isGame(w) && !isBar(w)) return;
    place(w);
    // GW2 (Wine) resizes its window many times while it loads, GTK sets its own size once: put it back
    // every time, but at most 10 times a second so a window that keeps fighting can't lock KWin up. Never
    // give up for good: once it has calmed down, a timer puts it in place once more.
    let since = 0, n = 0;
    const later = new QTimer();
    later.singleShot = true;
    later.interval = 500;
    later.timeout.connect(() => { const t = target(w); if (t && !same(w.frameGeometry, t)) place(w); });
    const fix = () => {
        const t = target(w);
        if (!t || same(w.frameGeometry, t)) return;
        const now = Date.now();
        if (now - since > 1000) { since = now; n = 0; }
        if (n++ < 10) place(w);
        else later.start();
    };
    w.frameGeometryChanged.connect(fix);
    w.captionChanged.connect(fix);
    w.fullScreenChanged.connect(fix);
}

workspace.windowAdded.connect(manage);
workspace.windowList().forEach(manage);
// The hotkeys (Meta+Shift+B mode, Meta+Shift+M marker) live in the always-loaded KWin script letterbox-hotkeys.
