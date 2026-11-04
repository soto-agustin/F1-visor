// Vercel Edge Function - Ultra-Low Bandwidth Live Endpoint
// Compatible con Upstash Redis REST API, ETags y HTTP 304

export const config = {
  runtime: 'edge',
};

export default async function handler(request) {
  const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
  const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;

  if (!redisUrl || !redisToken) {
    return new Response(JSON.stringify({ error: 'Upstash Redis configuration missing' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  try {
    // 1. Obtener simultáneamente race_state, race_state_hash y race_state_ts usando REST Pipeline
    const pipelineRes = await fetch(`${redisUrl}/pipeline`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${redisToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify([
        ['GET', 'race_state'],
        ['GET', 'race_state_hash'],
        ['GET', 'race_state_ts']
      ]),
    });

    if (!pipelineRes.ok) {
      throw new Error(`Upstash returned status ${pipelineRes.status}`);
    }

    const results = await pipelineRes.json();
    const rawState = results[0]?.result;
    const hash = results[1]?.result;
    const ts = results[2]?.result || '';

    if (!rawState) {
      return new Response(JSON.stringify({ message: 'No active session data' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const etag = `"${hash}"`;
    const clientIfNoneMatch = request.headers.get('if-none-match');

    // 2. Si el hash coincide con If-None-Match, retornar HTTP 304 (0 bytes de payload!)
    if (clientIfNoneMatch && clientIfNoneMatch === etag) {
      return new Response(null, {
        status: 304,
        headers: {
          ETag: etag,
          'X-TS': String(ts),
          'Cache-Control': 'no-cache',
        },
      });
    }

    // 3. Si hay cambio, devolver el payload completo con ETag y X-TS
    return new Response(rawState, {
      status: 200,
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        ETag: etag,
        'X-TS': String(ts),
        'Cache-Control': 'no-cache',
      },
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
