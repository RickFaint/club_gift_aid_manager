// A browser may restore an authenticated document from its back/forward cache.
// Reloading a persisted page forces the server to re-check the current session.
window.addEventListener('pageshow', (event) => {
  if (event.persisted) window.location.reload();
});
