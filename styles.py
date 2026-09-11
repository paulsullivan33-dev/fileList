import html
import socket

APP_TITLE = f"fileList {socket.gethostname()}"

CSS_JS = f"<title>{html.escape(APP_TITLE)}</title>" + """
<style>
body { font-family: system-ui, sans-serif; margin: 20px; }
.app-title { font-size: 24px; margin: 0 0 16px; }
.title-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 24px; margin-bottom: 16px; }
.title-row .app-title { margin: 0; }
.title-row .external-sites { margin: 0 0 0 auto; padding: 0; text-align: right; }
.destination-tabs { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 0; margin-bottom: 16px; border-bottom: 1px solid #8886; }
.destination-tabs .dir-btn, .destination-tab { display: inline-block; font: inherit; color: inherit; background: transparent; border: 0; border-radius: 5px; padding: 8px 10px; margin: 0; text-decoration: none; cursor: pointer; }
.destination-tabs .dir-btn:hover, .destination-tab:hover { background: #8882; }
.destination-tab[aria-expanded='true'] { background: #267a3d; color: white; }
.destination-tab small { opacity: .75; font-size: 11px; margin-left: 5px; }
.destination-separator { opacity: .55; }
.destination-path { overflow-wrap: anywhere; font-family: monospace; }
.header { flex-wrap: wrap; }

.header {
    background: #2b2b2b;
    color: white;
    padding: 12px 20px;
    border-radius: 8px;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 14px;
}

.header a {
    color: #4caf50;
    text-decoration: none;
    padding: 4px 10px;
    border-radius: 4px;
}
.header a.active { background: #4caf50; color: white; }
.header a:hover  { background: rgba(255,255,255,0.1); }

.dark-toggle { margin-left: auto; cursor: pointer; user-select: none; }
.logout-form { margin: 0; }
.logout-link {
    background: none;
    border: 0;
    color: #4caf50;
    cursor: pointer;
    font: inherit;
    padding: 4px 10px;
    border-radius: 4px;
}
.logout-link:hover { background: rgba(255,255,255,0.1); }

.external-sites {
    margin: -6px 0 12px;
    padding: 0 4px;
    font-size: 14px;
}
.external-sites a { color: #1976d2; text-decoration: none; }
.external-sites a:hover { text-decoration: underline; }
.external-sites span { color: #888; }

.breadcrumbs {
    margin-bottom: 14px;
    font-size: 14px;
}
.breadcrumbs a { color: #4caf50; text-decoration: none; }
.breadcrumbs a:hover { text-decoration: underline; }

.stats {
    background: #f0f0f0;
    padding: 6px 12px;
    border-radius: 4px;
    font-size: 13px;
    color: #555;
    margin-bottom: 12px;
    display: inline-block;
}

#dropZone {
    border: 1px dashed #888;
    padding: 8px 12px;
    text-align: left;
    border-radius: 6px;
    margin: 6px 0 10px;
    font-size: 13px;
    color: #888;
    transition: all 0.15s ease;
    cursor: pointer;
}
#dropZone.dragover {
    border-color: #4caf50;
    background: rgba(76, 175, 80, 0.1);
    color: #4caf50;
}
#uploadInput { display: none; }

.toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 14px;
    flex-wrap: wrap;
}
.toolbar-btn {
    padding: 8px 14px;
    background: #444;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    transition: background 0.15s;
}
.toolbar-btn:hover   { background: #555; }
.toolbar-btn.primary { background: #4caf50; }
.toolbar-btn.primary:hover { background: #45a049; }
.toolbar-btn.danger  { background: #d32f2f; }
.toolbar-btn.danger:hover  { background: #b71c1c; }

#searchInput {
    flex: 1;
    min-width: 180px;
    padding: 8px 12px;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 13px;
    margin-left: auto;
}

.table {
    width: 100%;
    table-layout: fixed;
    border-collapse: collapse;
    background: white;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.table th {
    background: #2b2b2b;
    color: white;
    padding: 10px 12px;
    text-align: left;
    font-size: 13px;
    user-select: none;
}
.table th.sortable { cursor: pointer; }
.table th.sortable:hover { background: #3b3b3b; }

.table td {
    padding: 8px 12px;
    border-bottom: 1px solid #eee;
    font-size: 14px;
}
.table tr:hover { background: #f7f7f7; }

.table th:first-child,
.table td:first-child { width: 32px; }
.table .preview-col { width: 88px; }
.table .size {
    width: 72px;
    white-space: nowrap;
}
.table .mtime {
    width: 122px;
    white-space: nowrap;
}
.table .actions {
    width: 345px;
    white-space: nowrap;
}
.table .filename {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.table .filename a {
    display: inline;
}

.row-hidden { opacity: 0.55; }
.row-dir    { font-weight: 500; }
.row-parent { color: #888; font-style: italic; }

#fileTable tbody tr.focused {
    background: rgba(76, 175, 80, 0.12) !important;
    outline: 2px solid #4caf50;
    outline-offset: -2px;
}

body:not(.show-hidden) .row-hidden { display: none; }

.icon { margin-right: 4px; }
.preview-img {
    max-width: 80px;
    max-height: 60px;
    border-radius: 3px;
    cursor: zoom-in;
    vertical-align: middle;
}

.actions a {
    color: #1976d2;
    text-decoration: none;
    font-size: 13px;
    margin-right: 4px;
}
.actions a:hover { text-decoration: underline; }

.statusbar {
    position: sticky;
    bottom: 0;
    background: #2b2b2b;
    color: white;
    padding: 6px 14px;
    font-size: 12px;
    border-radius: 4px 4px 0 0;
    margin-top: 14px;
}

#jobsPanel { position: fixed; right: 16px; bottom: 16px; z-index: 9000; }
.job-status {
    margin-top: 8px; padding: 10px 12px; min-width: 220px;
    background: #263238; color: white; border-radius: 6px;
    box-shadow: 0 2px 8px #0004;
}
.job-status button { float: right; margin-left: 12px; }

.toast {
    position: fixed;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%) translateY(20px);
    background: #333;
    color: white;
    padding: 10px 20px;
    border-radius: 6px;
    opacity: 0;
    transition: all 0.2s ease;
    z-index: 100000;
    pointer-events: none;
    font-size: 14px;
}
.toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
}

.action-box {
    background: #f9f9f9;
    padding: 20px;
    border-radius: 8px;
    margin-top: 14px;
}
.action-title { font-weight: bold; margin-bottom: 10px; }
.action-box ul { list-style: none; padding: 0; }
.action-box li { padding: 4px 0; }
.action-box a { color: #1976d2; text-decoration: none; }
.action-box a:hover { text-decoration: underline; }

.move-here-btn {
    display: inline-block;
    padding: 10px 20px;
    background: #4caf50;
    color: white !important;
    border-radius: 6px;
    text-decoration: none !important;
    font-size: 15px;
    font-weight: bold;
}
.move-here-btn:hover { background: #45a049; }
.move-here-top    { padding-bottom: 8px; border-bottom: 1px solid #ddd; margin-bottom: 10px; }
.move-here-bottom { padding-top: 8px;    border-top:    1px solid #ddd; margin-top: 10px; }

html.dark-mode body { background: #1a1a1a; color: #e0e0e0; }
html.dark-mode .table { background: #2b2b2b; }
html.dark-mode .table td { color: #e8e8e8; border-bottom-color: #3b3b3b; }
html.dark-mode .table tr:hover { background: #333; }
html.dark-mode .stats { background: #2b2b2b; color: #aaa; }
html.dark-mode .action-box { background: #2b2b2b; }
html.dark-mode #searchInput { background: #2b2b2b; color: #e0e0e0; border-color: #444; }
html.dark-mode .breadcrumbs a { color: #81c784; }
html.dark-mode .actions a { color: #64b5f6; }
html.dark-mode .external-sites a,
html.dark-mode .external-sites a:visited { color: #90caf9; }
html.dark-mode .external-sites span { color: #9ca3af; }
html.dark-mode .table .filename a,
html.dark-mode .table .filename a:visited { color: #f1f5f9; }
html.dark-mode .table .row-dir .filename a,
html.dark-mode .table .row-dir .filename a:visited { color: #9fe3a7; }
html.dark-mode .table .row-parent .filename a,
html.dark-mode .table .row-parent .filename a:visited { color: #c4cbd3; }
html.dark-mode .table .filename a:hover { color: #ffffff; text-decoration: underline; }
</style>

<script>
function toggleDarkMode() {
    document.documentElement.classList.toggle('dark-mode');
    try {
        localStorage.setItem('darkMode',
            document.documentElement.classList.contains('dark-mode') ? '1' : '0');
    } catch (e) {}
}
(function() {
    try {
        if (localStorage.getItem('darkMode') === '1') {
            document.documentElement.classList.add('dark-mode');
        }
    } catch (e) {}
})();
</script>
"""
