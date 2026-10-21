// Cloudflare Email Worker for support@, postmaster@, abuse@ and dmarc@mailvender.com.
//
// For every message: forward a backup copy to FORWARD_TO (your Gmail), then POST the raw
// message to the Mailvender API, signed with INBOUND_MAIL_SECRET:
//
//   POST {API_URL}/internal/inbound-mail?to=<address>
//   X-Mailvender-Signature: sha256=<HMAC-SHA256 of the body>
//
// The message then shows up in the admin app (Inbox). If the API is down, the Gmail copy
// still arrives, so nothing is lost. Setup: docs/ADMIN_SETUP.md, "Role inboxes".

const MAX_BYTES = 10 * 1024 * 1024;

async function sign(secret, body) {
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const signature = new Uint8Array(await crypto.subtle.sign('HMAC', key, body));
  return 'sha256=' + [...signature].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export default {
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();

    if (env.FORWARD_TO) {
      try {
        await message.forward(env.FORWARD_TO);
      } catch (error) {
        console.log(`forward to ${env.FORWARD_TO} failed: ${error}`);
      }
    }

    if (raw.byteLength > MAX_BYTES) {
      console.log(`${message.to}: ${raw.byteLength} bytes is too large for the admin inbox (Gmail copy only)`);
      return;
    }
    const response = await fetch(`${env.API_URL}/internal/inbound-mail?to=${encodeURIComponent(message.to)}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'message/rfc822',
        'X-Mailvender-Signature': await sign(env.INBOUND_MAIL_SECRET, raw),
      },
      body: raw,
    });
    if (!response.ok) {
      console.log(`API answered ${response.status} for ${message.to}: ${(await response.text()).slice(0, 300)}`);
    }
  },
};
