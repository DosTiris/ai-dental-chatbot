# MIA OFFICE PORTAL - AUTHENTICATION SETUP (P2 foundation)

Scope: server-side verification + tenant binding only. No portal pages, no
business features. Baseline: b46c3c9 (+ this P2 package).

## 1. Architecture (approved P2 design)

Supabase Auth owns identities, passwords, invites, and password reset.
The Mia backend NEVER sees a password, stores no tokens, and holds NO
Supabase service-role key. Flow:

    browser -> Supabase Auth (public anon key)  -> access token (JWT)
    browser -> Mia API: Authorization: Bearer <access token>
    Mia API -> verify signature/exp/aud -> sub -> office_users -> clients
            -> the ONE authorized office (server-side; browser never chooses)

`client_key` stays a public widget identifier. `ADMIN_API_KEY` stays
operator-only. `mia_cal_` keys stay the Calendar staff API credential.
None of the three can authenticate to /portal/*.

## 2. Server environment variables (backend only - never in a browser)

Exactly ONE of:

    SUPABASE_JWT_SECRET   Project "JWT Secret" (legacy/HS256 signing)
    SUPABASE_JWKS_URL     https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
                          (projects migrated to asymmetric signing keys)

Always required (F-P2-2):

    SUPABASE_AUTH_ISSUER  the EXACT project Auth issuer, e.g.
                          https://<project-ref>.supabase.co/auth/v1
                          Tokens whose iss claim is missing or different are
                          rejected with the standard 401.

Optional:

    PORTAL_JWT_AUDIENCE   default "authenticated"

Neither key source set, both set, or SUPABASE_AUTH_ISSUER missing -> every
/portal request returns 503 (fail closed).

## 3. Provider-side steps (Supabase dashboard - NOT performed by this package)

1. Authentication -> Providers -> Email: enabled; **Disable sign-ups**
   ("Allow new users to sign up" = OFF). Invited users can still accept
   invites; unrestricted public registration stays impossible.
1b. Authentication -> **Disable anonymous sign-ins** (F-P2-2). The backend
   additionally rejects any verified token carrying is_anonymous=true, so a
   Supabase anonymous session can never become an office identity even if
   this switch is ever flipped back on.
2. Authentication -> Email templates / SMTP: confirm invite + reset emails
   deliver (default SMTP acceptable for pilot).
3. Authentication -> URL configuration: set the future portal origin as the
   Site URL / redirect for invite + recovery links (finalized in P3 when the
   login page ships; placeholder acceptable now).
4. Copy the JWT Secret (Settings -> API) OR confirm the JWKS URL, and place
   it in the backend environment (Render), never in any frontend.

## 4. Provisioning runbook (Dos Tiris creates a known office user)

1. Supabase dashboard -> Authentication -> Users -> "Invite user" with the
   office's email. The office follows the email and CHOOSES ITS OWN
   PASSWORD (Dos Tiris never knows it).
2. Copy the new auth user's UUID from the dashboard.
3. Bind it to the office (SQL editor; 005-style raw operator SQL):

       INSERT INTO office_users (auth_user_id, client_id, role)
       VALUES ('<AUTH-USER-UUID>', '<CLIENT-UUID>', 'office_admin');

   V1 rules enforced by schema: one binding per auth user (unique), role
   vocabulary closed to 'office_admin'.
4. Deactivate later with:

       UPDATE office_users
       SET active = false, deactivated_at = now()
       WHERE auth_user_id = '<AUTH-USER-UUID>';

## 5. Password reset path

Supabase-native: "Forgot password" triggers Supabase's recovery email
(dashboard "Send password recovery", or in P3 the login page calls the
public recovery endpoint with the anon key). The office sets a new password
with Supabase; the Mia backend is not involved and needs no change.

## 6. Owner verification (after migration 007 + env configured)

    # obtain a token (HS256 projects; anon key is PUBLIC):
    curl -s "https://<ref>.supabase.co/auth/v1/token?grant_type=password" \
      -H "apikey: <ANON_KEY>" -H "Content-Type: application/json" \
      -d '{"email":"<office email>","password":"<their password>"}'

    # prove server-side tenant binding (no client_id anywhere):
    curl -s https://<mia-host>/portal/me \
      -H "Authorization: Bearer <access_token>"

Expected: 200 with client_id / practice_name / role / email for exactly the
bound office; 401 for every other credential including client_key,
ADMIN_API_KEY, and mia_cal_ keys.

## 6b. Rollout order + data exposure posture

MIGRATION BEFORE CODE (F-P2-3): apply 007 first, deploy the P2 application
second. office_users is not registered on the startup Base, so the app can
never auto-create it; deploying code before 007 simply 401s every portal
request until the migration runs (fail closed, no drift).

office_users carries ENABLED row level security with ZERO policies and
revoked anon/authenticated privileges (F-P2-1): browser/Data-API roles can
neither read nor write the binding table; only the owning backend role and
operators touch it.

## 7. Rollback

Application: revert the P2 files (package rollback instructions).
Schema: migrations/007_office_users_down.sql (export office_users first).
Supabase Auth users are untouched by rollback; without bindings every
portal request fails closed with 401.
