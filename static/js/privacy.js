/** Submit the admin-only text upload and update the displayed notice as text. */
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('privacy-notice-upload-form');
  if (!form) return;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!token) {
      window.alert('Could not verify this session. Refresh the page and try again.');
      return;
    }

    try {
      const response = await fetch('/admin/privacy-notice', {
        method: 'POST',
        headers: { 'X-CSRF-Token': token },
        body: new FormData(form),
      });
      const data = (response.headers.get('content-type') || '').includes('application/json')
        ? await response.json()
        : { error: 'The server returned an unexpected response.' };
      if (!response.ok) throw new Error(data.error || 'Unable to update the privacy statement.');

      // Use textContent so a notice containing markup is never interpreted as HTML.
      const content = document.getElementById('privacy-notice-content');
      if (content) content.textContent = data.notice;
      form.reset();
      window.alert('Privacy statement updated for all pages.');
    } catch (error) {
      window.alert(error.message || 'Unable to update the privacy statement.');
    }
  });
});
