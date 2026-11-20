// Application startup and page-level orchestration.

/**
 * Refresh every data-backed component that exists on the current page.
 * Each loader safely returns when its target markup is absent, which lets the base
 * template use one initialization path without issuing unrelated API requests.
 */
async function refreshPageData() {
  // Flask Blueprint endpoints are qualified (for example, "trips.trips_page").
  // Page orchestration uses the view function name so it also works for
  // unqualified endpoints rendered outside a Blueprint.
  const page = (document.body.dataset.page || '').split('.').pop();
  await loadPeople();
  if (page === 'mileage_rates_page') return loadMileageRateSchedules();
  if (page === 'index') {
    if (appRole !== Role.VOLUNTEER) {
      const summary = await api('/reports/summary');
      renderSummary(summary);
    }
    const overview = await api('/dashboard/overview');
    renderDashboardOverview(overview);
    return;
  }
  if (page === 'destinations_page') return loadDestinations();
  if (page === 'trips_page') {
    await loadDestinations();
    return loadTrips();
  }
  if (page === 'declarations_page') return loadDeclarations();
  if (page === 'donations_page') {
    await loadDeclarations();
    await loadPayments();
    return loadDonations();
  }
  if (page === 'payments_page') {
    await loadTrips();
    await loadPayments();
    return loadPaymentReconciliation();
  }
  if (page === 'reports_page') {
    await loadFinancialReport();
    return loadReportDataTables();
  }
  if (page === 'users_page') return loadUsers();
  if (page === 'audit_page') return loadAudit();
}

/** Populate the person edit dialog from the already-loaded directory record. */

// Wire form submission and delegated table actions once the page markup exists.
document.addEventListener('DOMContentLoaded', async () => {
  document.querySelectorAll('nav .nav-link.active').forEach((link) => link.setAttribute('aria-current', 'page'));
  let dialogTrigger = null;
  document.addEventListener('click', (event) => {
    const trigger = event.target.closest('[data-edit-person], [data-edit-destination], [data-edit-declaration], [data-edit-donation], [data-edit-user], [data-invite-user], [data-reset-password], [data-trip-corrections]');
    if (trigger) dialogTrigger = trigger;
  }, true);
  document.addEventListener('show.bs.modal', (event) => { dialogTrigger = event.relatedTarget || dialogTrigger; });
  document.addEventListener('hidden.bs.modal', () => {
    if (dialogTrigger?.isConnected) dialogTrigger.focus();
    dialogTrigger = null;
  });
  attachMileageRateForm();
  attachForm('person-form', '/people');
  attachCsvImportForm('people-import-form', '/people/import', 'person');
  attachUpdateForm('person-edit-form', '/people');
  attachForm('destination-form', '/destinations');
  attachCsvImportForm('destination-import-form', '/destinations/import', 'location');
  attachUpdateForm('destination-edit-form', '/destinations');
  attachForm('trip-form', '/trips');
  attachCsvImportForm('trip-import-form', '/trips/import', 'trip');
  attachForm('declaration-form', '/declarations');
  attachUpdateForm('declaration-edit-form', '/declarations');
  attachForm('donation-form', '/donations');
  attachUpdateForm('donation-edit-form', '/donations');
  attachForm('payment-form', '/payments');
  attachForm('user-form', '/admin/users');
  attachCsvImportForm('accounts-import-form', '/admin/users/import', 'account');

  const passwordResetForm = document.getElementById('password-reset-form');
  passwordResetForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const temporaryPassword = passwordResetForm.elements.namedItem('temporary_password').value;
    const confirmation = passwordResetForm.elements.namedItem('confirm_temporary_password').value;
    if (temporaryPassword !== confirmation) {
      alert('The temporary password and confirmation do not match.');
      return;
    }
    try {
      await api(`/admin/users/${passwordResetForm.elements.namedItem('user_id').value}/password-reset`, {
        method: 'POST',
        body: JSON.stringify({ temporary_password: temporaryPassword }),
      });
      passwordResetForm.reset();
      bootstrap.Modal.getOrCreateInstance(document.getElementById('password-reset-modal')).hide();
      await refreshPageData();
      alert('Temporary password set. Share it securely; the user must replace it at next login.');
    } catch (error) {
      alert(error.message || 'Unable to reset the password.');
    }
  });
  if (passwordResetForm) enableHandledForm(passwordResetForm);

  const userRole = document.getElementById('user-role-select');
  const personField = document.getElementById('user-person-field');
  if (userRole && personField) {
    const updateVisibility = () => personField.classList.toggle('d-none', userRole.value === Role.ADMIN);
    userRole.addEventListener('change', updateVisibility);
    updateVisibility();
  }
  document.getElementById('donation-person-select')?.addEventListener('change', () => {
    Promise.all([
      loadPaymentChoices('donation-person-select', 'donation-payment-select'),
      loadDeclarationChoices('donation-person-select', 'donation-declaration-select'),
    ]).catch((error) => alert(error.message));
  });
  document.getElementById('donation-edit-person-select')?.addEventListener('change', () => {
    Promise.all([
      loadPaymentChoices('donation-edit-person-select', 'donation-edit-payment-select'),
      loadDeclarationChoices('donation-edit-person-select', 'donation-edit-declaration-select'),
    ]).catch((error) => alert(error.message));
  });
  document.getElementById('payment-person-select')?.addEventListener('change', fillPaymentTripSelect);
  document.getElementById('payment-trip-select')?.addEventListener('change', updatePaymentAmount);
  document.getElementById('trip-origin-select')?.addEventListener('change', updateTripMileage);
  document.getElementById('trip-destination-select')?.addEventListener('change', updateTripMileage);
  document.querySelectorAll('[data-table-search]').forEach((search) => {
    search.addEventListener('input', () => applyTableSearch(search.dataset.tableSearch));
  });
  document.querySelectorAll('[data-report-date-filter]').forEach((filter) => {
    filter.addEventListener('input', () => filterReportTable(filter.dataset.reportDateFilter));
  });
  document.querySelectorAll('[data-report-filter]').forEach((filter) => {
    filter.addEventListener('change', () => filterReportTable(filter.dataset.reportFilter));
  });
  document.getElementById('summary-volunteer-filter')?.addEventListener('change', filterVolunteerSummary);

  const userEditForm = document.getElementById('user-edit-form');
  userEditForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const userId = Number(userEditForm.dataset.userId);
    if (!userId) return;
    const payload = Object.fromEntries(new FormData(userEditForm).entries());
    payload.is_active = payload.is_active === 'true';
    if (!payload.person_id) payload.person_id = null;
    if (payload.role === Role.ADMIN) payload.person_id = null;
    try {
      await api(`/admin/users/${userId}`, { method: 'PATCH', body: JSON.stringify(payload) });
      bootstrap.Modal.getOrCreateInstance(document.getElementById('user-edit-modal')).hide();
      await refreshPageData();
    } catch (error) {
      const message = document.getElementById('user-edit-error');
      message.textContent = error.message;
      message.hidden = false;
    }
  });
  if (userEditForm) enableHandledForm(userEditForm);

  let invitationUserId = 0;
  const invitationForm = document.getElementById('user-invitation-form');
  invitationForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!invitationUserId) return;
    try {
      const result = await api(`/admin/users/${invitationUserId}/invitation`, { method: 'POST' });
      document.getElementById('user-invitation-code').value = result.code;
      document.getElementById('user-invitation-link').href = result.activation_path;
      document.getElementById('user-invitation-details').hidden = false;
    } catch (error) {
      const message = document.getElementById('user-invitation-error');
      message.textContent = error.message;
      message.hidden = false;
    }
  });
  if (invitationForm) enableHandledForm(invitationForm);
  document.querySelectorAll('[data-export-table]').forEach((button) => {
    button.addEventListener('click', () => {
      exportReportTable(button.dataset.exportTable, button.dataset.exportFilename);
    });
  });

  document.addEventListener('click', async (event) => {
    const editUserButton = event.target.closest('[data-edit-user]');
    if (editUserButton) {
      const user = state.users.find((entry) => entry.id === Number(editUserButton.dataset.editUser));
      if (!user || !userEditForm) return;
      userEditForm.dataset.userId = String(user.id);
      userEditForm.elements.role.value = user.role;
      userEditForm.elements.person_id.value = user.person_id || '';
      userEditForm.elements.is_active.value = String(Boolean(user.is_active));
      document.getElementById('user-edit-name').textContent = user.username;
      document.getElementById('user-edit-error').hidden = true;
      bootstrap.Modal.getOrCreateInstance(document.getElementById('user-edit-modal')).show();
      return;
    }
    const inviteUserButton = event.target.closest('[data-invite-user]');
    if (inviteUserButton) {
      const user = state.users.find((entry) => entry.id === Number(inviteUserButton.dataset.inviteUser));
      if (!user || !invitationForm) return;
      invitationUserId = user.id;
      document.getElementById('user-invitation-name').textContent = user.username;
      document.getElementById('user-invitation-details').hidden = true;
      document.getElementById('user-invitation-code').value = '';
      document.getElementById('user-invitation-error').hidden = true;
      bootstrap.Modal.getOrCreateInstance(document.getElementById('user-invitation-modal')).show();
      return;
    }
    const resetPasswordButton = event.target.closest('[data-reset-password]');
    if (resetPasswordButton) {
      document.getElementById('password-reset-user-id').value = resetPasswordButton.dataset.resetPassword;
      document.getElementById('password-reset-username').textContent = resetPasswordButton.dataset.resetUsername;
      bootstrap.Modal.getOrCreateInstance(document.getElementById('password-reset-modal')).show();
      return;
    }
    const resetMfaButton = event.target.closest('[data-reset-mfa]');
    if (resetMfaButton) {
      const confirmed = window.confirm(
        "Reset MFA only after verifying this person through the club's approved offline process. Continue?",
      );
      if (!confirmed) return;
      try {
        await api(`/admin/users/${resetMfaButton.dataset.resetMfa}/mfa-reset`, {
          method: 'POST',
        });
        await refreshPageData();
      } catch (error) {
        alert(error.message || 'Unable to reset MFA.');
      }
      return;
    }
    const editDeclarationButton = event.target.closest('[data-edit-declaration]');
    if (editDeclarationButton) return editDeclaration(Number(editDeclarationButton.dataset.editDeclaration));
    const toggleDeclarationButton = event.target.closest('[data-toggle-declaration]');
    if (toggleDeclarationButton) {
      try {
        const declarationId = toggleDeclarationButton.dataset.toggleDeclaration;
        const active = toggleDeclarationButton.dataset.active === 'true';
        if (active) {
          await api(`/declarations/${declarationId}`, {
            method: 'PUT',
            body: JSON.stringify({ active: true }),
          });
        } else {
          await api(`/declarations/${declarationId}`, { method: 'DELETE' });
        }
        await refreshPageData();
      } catch (error) {
        alert(error.message || 'Unable to update declaration status.');
      }
      return;
    }
    const editDonationButton = event.target.closest('[data-edit-donation]');
    if (editDonationButton) return editDonation(Number(editDonationButton.dataset.editDonation));
    const toggleDonationButton = event.target.closest('[data-toggle-donation]');
    if (toggleDonationButton) {
      try {
        await api(`/donations/${toggleDonationButton.dataset.toggleDonation}`, {
          method: 'PUT',
          body: JSON.stringify({ active: toggleDonationButton.dataset.active === 'true' }),
        });
        await refreshPageData();
      } catch (error) {
        alert(error.message || 'Unable to update donation status.');
      }
      return;
    }
    const editButton = event.target.closest('[data-edit-person]');
    if (editButton) return editPerson(Number(editButton.dataset.editPerson));
    const deletePersonButton = event.target.closest('[data-delete-person]');
    if (deletePersonButton) {
      return removeRecord(`/people/${deletePersonButton.dataset.deletePerson}`, 'Remove this person\u2019s identifying details? Their trip distances and financial transaction history will be kept, but the person and their linked declarations will be anonymized. Any linked account will be deactivated.');
    }
    const deleteTripButton = event.target.closest('[data-delete-trip]');
    if (deleteTripButton) {
      return removeRecord(`/trips/${deleteTripButton.dataset.deleteTrip}`, 'Delete this trip record?');
    }
    const editDestinationButton = event.target.closest('[data-edit-destination]');
    if (editDestinationButton) return editDestination(Number(editDestinationButton.dataset.editDestination));
    const deleteDestinationButton = event.target.closest('[data-delete-destination]');
    if (deleteDestinationButton) {
      return removeRecord(`/destinations/${deleteDestinationButton.dataset.deleteDestination}`, 'Delete this destination? Destinations already used by trips cannot be deleted; deactivate them instead.');
    }
  });

  try {
    await refreshPageData();
  } catch (error) {
    alert(error.message || 'Unable to load this page.');
  }
});
