// letterbox hotkeys: global shortcuts that work in fullscreen games too. They call the stats bar over
// D-Bus; while it doesn't run (no game) nothing happens.
registerShortcut("letterbox: bar mode", "letterbox: bar mode (full / compact / clock / off)", "Meta+Shift+B", () => {
    callDBus("letterbox.bar", "/letterbox/bar", "letterbox.Bar", "Cycle");
});
registerShortcut("letterbox: lag marker", "letterbox: lag marker", "Meta+Shift+M", () => {
    callDBus("letterbox.bar", "/letterbox/bar", "letterbox.Bar", "Mark");
});
