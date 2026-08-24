const ledger = document.querySelector('#ledger');
const template = document.querySelector('#job-template');
const message = document.querySelector('#message');
const processButton = document.querySelector('#process-button');
const stopButton = document.querySelector('#stop-button');
let wasRunning = false;

async function request(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function showMessage(text) { message.textContent = text; }

async function loadJobs() {
  const filter = document.querySelector('#review-filter').value;
  const jobs = await request(`/api/jobs?review=${encodeURIComponent(filter)}`);
  ledger.replaceChildren();
  document.querySelector('#empty').hidden = jobs.length !== 0;
  for (const job of jobs) ledger.append(renderJob(job));
}

function renderJob(job) {
  const card = template.content.firstElementChild.cloneNode(true);
  card.dataset.review = job.review_status;
  card.querySelector('.job-number').textContent = `Record ${String(job.id).padStart(5, '0')}`;
  const pill = card.querySelector('.status-pill');
  pill.textContent = `${job.status} · ${job.review_status}`;
  pill.dataset.status = job.status;
  const source = card.querySelector('.source-image');
  source.src = job.source_url;
  source.alt = `Source photograph ${job.source_name}`;
  const result = card.querySelector('.result-image');
  if (job.result_url) {
    result.src = `${job.result_url}?v=${encodeURIComponent(job.updated_at)}`;
    result.alt = `Generated stamp for ${job.source_name}`;
    card.querySelector('.result-placeholder').hidden = true;
  }
  card.querySelector('.filename').textContent = job.source_name;
  card.querySelector('.error').textContent = job.error || '';
  const note = card.querySelector('.note');
  note.value = job.note || '';
  card.querySelector('.approve').disabled = job.status !== 'complete';
  card.querySelector('.reject').disabled = job.status !== 'complete';
  card.querySelector('.approve').addEventListener('click', () => review(job.id, 'approved', note.value));
  card.querySelector('.reject').addEventListener('click', () => review(job.id, 'rejected', note.value));
  card.querySelector('.regenerate').addEventListener('click', () => regenerate(job.id, note.value));
  return card;
}

async function review(id, reviewStatus, note) {
  await request(`/api/jobs/${id}/review`, {method: 'POST', body: JSON.stringify({review_status: reviewStatus, note})});
  showMessage(`Record ${id} marked ${reviewStatus}.`);
  await Promise.all([loadJobs(), loadStats()]);
}

async function regenerate(id, note) {
  await request(`/api/jobs/${id}/regenerate`, {method: 'POST', body: JSON.stringify({note})});
  showMessage(`Record ${id} queued. Its existing files were preserved until replacement succeeds.`);
  await Promise.all([loadJobs(), loadStats()]);
}

async function loadStats() {
  const stats = await request('/api/stats');
  const complete = stats.statuses.complete || 0;
  document.querySelector('#progress-count').textContent = `${complete} / ${stats.total}`;
  document.querySelector('#progress-bar').style.width = `${stats.total ? complete / stats.total * 100 : 0}%`;
  document.querySelector('#worker-state').textContent = stats.stopping
    ? 'Stopping after current image…'
    : stats.running
      ? 'Press in progress…'
      : `${stats.reviews.approved || 0} approved · ${stats.reviews.rejected || 0} rejected`;
  const keyState = document.querySelector('#key-state');
  keyState.dataset.ready = String(stats.api_key_available);
  keyState.textContent = stats.api_key_available ? 'API key available to server' : 'API key missing — restart server after export';
  processButton.disabled = stats.running || !stats.api_key_available;
  stopButton.disabled = !stats.running || stats.stopping;
  if (wasRunning && !stats.running) {
    showMessage('Processing finished. Review the new proofs.');
    await loadJobs();
  }
  wasRunning = stats.running;
}

document.querySelector('#scan-button').addEventListener('click', async () => {
  try {
    const result = await request('/api/scan', {method: 'POST'});
    showMessage(`Found ${result.found}: ${result.added} added, ${result.updated} changed, ${result.missing} no longer in source.`);
    await Promise.all([loadJobs(), loadStats()]);
  } catch (error) { showMessage(error.message); }
});

processButton.addEventListener('click', async () => {
  try {
    const limit = Number(document.querySelector('#batch-limit').value);
    await request('/api/process', {method: 'POST', body: JSON.stringify({limit})});
    showMessage(`Started up to ${limit} impressions.`);
    await loadStats();
  } catch (error) { showMessage(error.message); }
});

stopButton.addEventListener('click', async () => {
  try {
    const result = await request('/api/stop', {method: 'POST'});
    showMessage(result.message);
    await loadStats();
  } catch (error) { showMessage(error.message); }
});

document.querySelector('#review-filter').addEventListener('change', loadJobs);
Promise.all([loadJobs(), loadStats()]).catch(error => showMessage(error.message));
setInterval(() => loadStats().catch(error => showMessage(error.message)), 2500);
