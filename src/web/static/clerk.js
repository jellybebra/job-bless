/* Clerk refreshes its short-lived session cookie while the workspace is open. */
import { ruRU } from './clerk-ru.mjs';
window.addEventListener('load', async () => {
  const root = document.getElementById('clerk-sign-in');
  const status = document.getElementById('clerk-status');
  try {
    await Clerk.load({
      ui: { ClerkUI: window.__internal_ClerkUICtor },
      localization: ruRU,
      signInUrl: '/login', signUpUrl: '/signup',
      signInForceRedirectUrl: '/actions', signUpForceRedirectUrl: '/actions',
      afterSignOutUrl: '/login',
    });
    if (root) {
      if (Clerk.isSignedIn) {
        await Clerk.session.getToken({ skipCache: true });
        window.location.replace('/actions');
      } else {
        if (status) status.textContent = '';
        const options = { routing: 'hash', forceRedirectUrl: '/actions' };
        if (window.location.pathname === '/signup') Clerk.mountSignUp(root, options);
        else Clerk.mountSignIn(root, options);
      }
    } else if (!Clerk.isSignedIn) {
      window.location.replace('/login');
    } else {
      document.querySelectorAll('[data-clerk-user]').forEach(node => Clerk.mountUserButton(node));
      Clerk.addListener(({ session }) => { if (!session) window.location.replace('/login'); });
    }
  } catch (error) {
    if (status) status.textContent = 'Не удалось загрузить вход. Обновите страницу или попробуйте позже.';
  }
});
