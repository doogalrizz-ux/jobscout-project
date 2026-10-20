// JobScout morning trigger: starts the GitHub "Daily scan" workflow on a Cloudflare cron,
// because GitHub's own scheduled runs can be delayed or skipped.
//
// Each tick: skip if today's data is already published or a scan is already queued/running;
// otherwise dispatch the workflow. Needs secret GITHUB_TOKEN (fine-grained: Actions read/write
// on the repo only) and vars OWNER, REPO, WORKFLOW.

const GH = "https://api.github.com";

async function gh(env, path, init = {}) {
  return fetch(`${GH}/repos/${env.OWNER}/${env.REPO}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "jobscout-trigger",
      ...(init.headers || {}),
    },
  });
}

async function publishedToday(env) {
  const res = await fetch(`https://${env.OWNER}.github.io/${env.REPO}/data/jobs.json`, { cf: { cacheTtl: 0 } });
  if (!res.ok) return false;
  const { generated } = await res.json();
  return new Date(generated).toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10);
}

async function scanActive(env) {
  for (const status of ["queued", "in_progress"]) {
    const res = await gh(env, `/actions/workflows/${env.WORKFLOW}/runs?status=${status}&per_page=1`);
    if (res.ok && (await res.json()).total_count > 0) return true;
  }
  return false;
}

async function tick(env) {
  if (await publishedToday(env)) return "skip: today's data already published";
  if (await scanActive(env)) return "skip: a scan is already queued or running";
  const res = await gh(env, `/actions/workflows/${env.WORKFLOW}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main", inputs: { discover: "false" } }),
  });
  return res.status === 204 ? "dispatched" : `dispatch failed: ${res.status} ${await res.text()}`;
}

export default {
  async scheduled(event, env, ctx) {
    console.log(await tick(env));
  },
};
