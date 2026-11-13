/** Submit admin-entered or uploaded notice text and update the display as plain text. */
document.addEventListener('DOMContentLoaded', () => {
  const editorForm = document.getElementById('privacy-notice-editor-form');
  const uploadForm = document.getElementById('privacy-notice-upload-form');
  const editor = document.getElementById('privacy-notice-editor');
  const status = document.getElementById('privacy-notice-status');

  const saveNotice = async (event, form, isUpload) => {
    event.preventDefault();
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!token) {
      reportError('Could not verify this session. Refresh the page and try again.');
      return;
    }

    try {
      const body = isUpload
        ? new FormData(form)
        : JSON.stringify({ notice: editor.value });
      const response = await fetch('/admin/privacy-notice', {
        method: 'POST',
        headers: {
          'X-CSRF-Token': token,
          ...(isUpload ? {} : { 'Content-Type': 'application/json' }),
        },
        body,
      });
      const data = (response.headers.get('content-type') || '').includes('application/json')
        ? await response.json()
        : { error: 'The server returned an unexpected response.' };
      if (!response.ok) throw new Error(data.error || 'Unable to update the privacy statement.');

      // Use textContent so a notice containing markup is never interpreted as HTML.
      const content = document.getElementById('privacy-notice-content');
      if (content) content.textContent = data.notice;
      if (editor) editor.value = data.notice;
      if (uploadForm) uploadForm.reset();
      reportStatus('Privacy statement updated for all pages.', false);
    } catch (error) {
      reportError(error.message || 'Unable to update the privacy statement.');
    }
  };

  const reportStatus = (message, isError) => {
    if (status) {
      status.textContent = message;
      status.classList.toggle('text-danger', isError);
      status.classList.toggle('text-success', !isError);
    } else {
      window.alert(message);
    }
  };

  const reportError = (message) => reportStatus(message, true);

  if (editorForm && editor) {
    editorForm.addEventListener('submit', (event) => saveNotice(event, editorForm, false));
  }
  if (uploadForm) {
    uploadForm.addEventListener('submit', (event) => saveNotice(event, uploadForm, true));
  }
});
