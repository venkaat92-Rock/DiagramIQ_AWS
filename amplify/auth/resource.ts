import { defineAuth } from '@aws-amplify/backend';

/**
 * Who may use DiagramIQ.
 *
 * Accounts are created by a super admin, never by self sign-up: this is an
 * internal review tool, and an open registration page on a public URL is an
 * invitation to strangers. Cognito emails each new address an invitation with
 * a temporary password; the first sign-in forces the person to set their own,
 * which is the "set up your password" step in the invite mail.
 *
 * Two groups:
 *   superadmin  can invite, disable and re-invite people, and read the log
 *   user        can use the tool
 *
 * Group membership is carried in the token, so the admin function checks the
 * caller's claims rather than a list it keeps itself.
 */
export const auth = defineAuth({
  loginWith: {
    email: {
      userInvitation: {
        emailSubject: 'Your DiagramIQ account',
        emailBody: (user, code) =>
          `<p>You have been given access to DiagramIQ.</p>
           <p><b>Username:</b> ${user()}<br/>
              <b>Temporary password:</b> ${code()}</p>
           <p>Open DiagramIQ, sign in with the details above, and you will be asked
              to set a password of your own. The temporary one stops working then.</p>
           <p>The invitation expires in 7 days. If it lapses, ask your administrator
              to send another.</p>`,
      },
    },
  },
  groups: ['superadmin', 'user'],
  accountRecovery: 'EMAIL_ONLY',
});
