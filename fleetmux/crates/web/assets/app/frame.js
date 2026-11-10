// fleetmux GUI: the script injected into every sub-frame (design.md §3.6.4, PROTOCOL.md §15.4). The window process
// wraps it as `(<this function>)(<prefix spec>);` and injects it with with_initialization_script_for_main_only(js,
// false), so it also runs in the terminal page itself, where it does nothing. In the history iframe it:
//  - catches the prefix key in the capture phase and tells the terminal page ({fm:"prefix"}), so the web page never
//    eats it;
//  - reports the iframe's address on load / hashchange / popstate ({fm:"url",href,why,ok}) and DOM focus coming in
//    ({fm:"history_focus"});
//  - reloads itself when the terminal page asks ({fm:"reload"}, checked to come from the parent).
// It holds no secret: the ipc token never reaches a sub-frame.
(function (prefix) {
  'use strict';
  if (window.top === window || window.parent !== window.top) return; // only the terminal page's own iframe
  var parent = window.parent;
  var post = function (m) { try { parent.postMessage(m, '*'); } catch (e) { /* parent gone */ } };
  var isPrefix = function (e) {
    if (!prefix || !e.ctrlKey || e.metaKey || e.altKey) return false;
    var k = String(e.key || '').toLowerCase();
    return k === prefix.key || e.code === prefix.code;
  };
  window.addEventListener('keydown', function (e) {
    if (!isPrefix(e)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    post({ fm: 'prefix' });
  }, true);
  var errorPage = function () {
    return !!document.querySelector('meta[name="fleetmux-error"]');
  };
  window.addEventListener('load', function () {
    post({ fm: 'url', href: location.href, why: 'load', ok: !errorPage() });
  });
  window.addEventListener('hashchange', function () { post({ fm: 'url', href: location.href, why: 'hashchange' }); });
  window.addEventListener('popstate', function () { post({ fm: 'url', href: location.href, why: 'popstate' }); });
  window.addEventListener('focus', function () { post({ fm: 'history_focus' }); });
  window.addEventListener('message', function (e) {
    if (e.source !== parent) return;
    var m = e.data;
    if (m && m.fm === 'reload') location.reload();
  });
})
