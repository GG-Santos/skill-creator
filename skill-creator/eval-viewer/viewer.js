const STORAGE_KEY = 'eval-review-v11-state';
const MAX_PREVIEW = 120000;
const TEXT_PREVIEW_CHARS = 65000;
const TABLE_PREVIEW_ROWS = 80;
const TABLE_PREVIEW_COLS = 12;
const KNOWN_OUTPUT_TYPES = new Set(['text', 'json', 'image', 'pdf', 'xlsx', 'csv', 'tsv', 'binary', 'error']);
const RETIRED_BUNDLE_SCHEMA = /^eval-review-[789]\.0$/;
const STATUS_FILTER_OPTIONS = [['all', 'All'], ['unreviewed', 'Unreviewed'], ['approved', 'Approved'], ['needs_changes', 'Needs'], ['blocked', 'Blocked']];
const SEVERITY_FILTER_OPTIONS = [['all', 'All'], ['none', 'None'], ['unknown', 'Unknown'], ['minor', 'Minor'], ['major', 'Major'], ['critical', 'Critical']];
const FILE_DECISION_OPTIONS = [['unreviewed', 'Unreviewed'], ['accepted', 'Accepted'], ['skipped', 'Skipped'], ['issue', 'Issue'], ['blocked', 'Blocked']];
const SEVERITY_RANK = { none: 0, unknown: 0.5, minor: 1, major: 2, critical: 3 };
const CHECKLIST_ITEMS = [
  ['output_inspected', 'Output inspected'],
  ['grades_checked', 'Grades checked'],
  ['previous_output_checked', 'Previous context checked'],
  ['benchmark_checked', 'Benchmark checked'],
  ['feedback_decision', 'Feedback decision recorded']
];
const CHECKLIST_LABELS = Object.fromEntries(CHECKLIST_ITEMS);
const RETIRED_DEFAULT_SNIPPETS = [
  'Approved. No changes needed.',
  'Needs changes: ',
  'Blocked: ',
  'Skipped. Severity unknown.'
];
const DEFAULT_SNIPPETS = [];
const FEEDBACK_QUALITY_RULES = [
  { label: 'Feedback contains mojibake or replacement characters', regex: /(?:\uFFFD|\u00C3[\u0080-\u00BF]|\u00C2[\u0080-\u00BF]?|\u00E2[\u0080-\u00BF]{1,2}|\u00F0\u0178)/ },
  { label: 'Feedback contains unsupported control characters', regex: /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/ },
  { label: 'Feedback looks like repeated garbage characters', regex: /([^\s])\1{7,}|[!?.,;:_\-*=~#@$%^&|\\/]{8,}/ },
  { label: 'Feedback looks like keyboard-mashing garble', regex: /\b(?:asdf|qwer|zxcv|hjkl|qaz|wsx|edc|rfv|tgb|yhn|ujm){2,}\b/i },
  { label: 'Feedback repeats the same word like spam', regex: /\b([a-z][a-z0-9_-]{2,})\b(?:\W+\1\b){3,}/i },
  { label: 'Feedback contains repeated URLs', regex: /(?:https?:\/\/|www\.)\S+(?:[\s\S]*(?:https?:\/\/|www\.)\S+)/i },
  { label: 'Feedback contains spam language', regex: /\b(?:click here|free money|buy now|subscribe now|promo code|casino|work from home|limited offer)\b/i }
];
const SECRET_WARNING_RULES = [
  { label: 'Feedback may contain an API token', regex: /\bsk-[A-Za-z0-9_-]{16,}\b/i },
  { label: 'Feedback may contain an API key assignment', regex: /\bapi[-_ ]?key\b\s*[:=]\s*['"]?[A-Za-z0-9._~+/=-]{12,}/i },
  { label: 'Feedback may contain a bearer token', regex: /\bbearer\s+[A-Za-z0-9._~+/=-]{16,}/i },
  { label: 'Feedback may contain a private key block', regex: /-----BEGIN [A-Z ]*PRIVATE KEY-----/i },
  { label: 'Feedback may contain cloud credentials', regex: /\b(?:aws_access_key_id|aws_secret_access_key|google_application_credentials|azure_client_secret)\b/i }
];
const EMPTY_DATA = {
  skill_name: 'Eval review',
  iteration: null,
  runs: [],
  previous_feedback: {},
  previous_outputs: {},
  benchmark: null
};

const BOOTSTRAP_DATA = window.EVAL_REVIEW_BOOTSTRAP || EMPTY_DATA;
const SERVER_FEEDBACK = window.EVAL_REVIEW_FEEDBACK || null;
let hadLocalState = false;
let data = normalizeData(BOOTSTRAP_DATA);
let state = loadState();
if (!hadLocalState && SERVER_FEEDBACK) restoreBundleState(SERVER_FEEDBACK);
for (const run of data.runs) syncOutputChecklist(run);
let saveTimer = null;
let serverSaveTimer = null;
let lastModalFocus = null;
let toastTimer = null;
let revealTimer = null;
let revealState = null;
let lastCompetenceToastKey = '';

function normalizeData(input) {
  if (input?.schemaVersion === 'eval-review-11.0' && input.source) return normalizeData(input.source);
  if (RETIRED_BUNDLE_SCHEMA.test(input?.schemaVersion || '') && input.source) {
    return normalizeData(input.source);
  }
  if (input?.workspace?.runs) {
    return {
      skill_name: input.workspace.skillName || input.workspace.name || 'Imported review workspace',
      iteration: input.workspace.iteration || input.iteration || null,
      runs: input.workspace.runs.map(normalizeRun),
      previous_feedback: input.previous_feedback || {},
      previous_outputs: normalizePreviousOutputs(input.previous_outputs || {}),
      benchmark: input.workspace.benchmark || input.benchmark || null
    };
  }
  return {
    skill_name: input?.skill_name || input?.name || 'Eval review',
    iteration: input?.iteration || null,
    runs: Array.isArray(input?.runs) ? input.runs.map(normalizeRun) : [],
    previous_feedback: input?.previous_feedback || {},
    previous_outputs: normalizePreviousOutputs(input?.previous_outputs || {}),
    benchmark: input?.benchmark || null
  };
}
function normalizeRun(run) {
  return {
    id: run.id || cryptoId('run'),
    scenario_id: run.scenario_id || run.scenarioId || '',
    variant: run.variant || '',
    source_run_id: run.source_run_id || run.sourceRunId || '',
    configuration: run.configuration || run.config || '',
    prompt: run.prompt || '',
    outputs: normalizeOutputs(Array.isArray(run.outputs) && run.outputs.length ? run.outputs : [{ name: `${run.id || 'run'}.txt`, type: 'text', content: run.output || '' }]),
    grading: run.grading || run.grades || null,
    benchmark: run.benchmark || null,
    benchmark_regression: Boolean(run.benchmark_regression)
  };
}
function normalizeOutputs(outputs) { return outputs.map(file => ({ ...file, type: fileType(file) })); }
function normalizePreviousOutputs(previous) {
  const result = {};
  for (const [runId, value] of Object.entries(previous || {})) {
    if (Array.isArray(value)) result[runId] = normalizeOutputs(value);
    else if (typeof value === 'string') result[runId] = [{ name: 'previous-output.txt', type: 'text', content: value }];
    else if (value && typeof value === 'object') result[runId] = normalizeOutputs([value]);
  }
  return result;
}
function initialState() {
  return {
    schemaVersion: 'eval-review-11.0',
    dataFingerprint: fingerprintData(data),
    currentIndex: 0,
    tab: 'review',
    search: '',
    filter: 'all',
    flowFilter: 'all',
    statusFilter: 'all',
    severityFilter: 'all',
    bookmarkedOnly: false,
    sortDirection: 'asc',
    visited: [],
    feedback: {},
    status: {},
    severity: {},
    timestamps: {},
    checklist: {},
    fileReviews: {},
    bookmarks: {},
    fileBookmarks: {},
    fileCursorByRunId: {},
    expandedOutputs: {},
    collapsedFiles: {},
    snippets: [...DEFAULT_SNIPPETS],
    activeSnippetIndex: '',
    snippetsOpen: false,
    bulkActions: [],
    lastBulkUndo: null,
    comparisonMode: 'unified',
    mobileSheetOpen: false,
    theme: window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  };
}
function fingerprintData(d) { return `${d.skill_name}|${d.runs.map(r => r.id).join(',')}|${d.runs.length}`; }
function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return initialState();
    const parsed = JSON.parse(raw);
    if (parsed.schemaVersion !== 'eval-review-11.0') return initialState();
    if (parsed.dataFingerprint !== fingerprintData(data)) return initialState();
    hadLocalState = true;
    const snippets = Array.isArray(parsed.snippets) ? cleanSnippets(parsed.snippets) : [...DEFAULT_SNIPPETS];
    const loaded = { ...initialState(), ...parsed, checklist: parsed.checklist || {}, fileReviews: parsed.fileReviews || {}, bookmarks: parsed.bookmarks || {}, fileBookmarks: parsed.fileBookmarks || {}, fileCursorByRunId: parsed.fileCursorByRunId || {}, expandedOutputs: parsed.expandedOutputs || {}, collapsedFiles: parsed.collapsedFiles || {}, snippets, activeSnippetIndex: normalizeSnippetIndex(parsed.activeSnippetIndex, snippets) };
    if (loaded.statusFilter === 'open' || loaded.statusFilter === 'complete') {
      loaded.flowFilter = loaded.statusFilter;
      loaded.statusFilter = 'all';
    }
    return loaded;
  } catch { return initialState(); }
}
function setSaveState(label, saving = false) {
  const save = document.getElementById('save-state');
  if (!save) return;
  save.innerHTML = iconSave();
  save.setAttribute('aria-label', label);
  save.dataset.tooltip = label;
  save.classList.toggle('is-saving', saving);
}
function canUseServerSave() {
  return window.EVAL_REVIEW_DISABLE_SERVER_SAVE !== true && /^https?:$/.test(window.location.protocol);
}
function persist() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  if (canUseServerSave()) queueServerSave('in_progress');
  else setSaveState('Autosaved');
}
function persistSoon() {
  clearTimeout(saveTimer);
  setSaveState('Saving', true);
  saveTimer = setTimeout(persist, 180);
}
function queueServerSave(status = 'in_progress', payload = null) {
  if (!canUseServerSave()) { setSaveState('Autosaved'); return; }
  clearTimeout(serverSaveTimer);
  setSaveState('Saving', true);
  serverSaveTimer = setTimeout(() => saveFeedbackToServer(status, payload), 260);
}
async function saveFeedbackToServer(status = 'in_progress', payload = null) {
  if (!canUseServerSave()) return false;
  try {
    const body = payload || buildFeedbackPayload(status);
    const resp = await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!resp.ok) throw new Error(`Save failed: ${resp.status}`);
    setSaveState('Autosaved');
    return true;
  } catch {
    setSaveState('Saved locally');
    return false;
  }
}
function cryptoId(prefix) { return `${prefix}_${Math.random().toString(36).slice(2, 9)}`; }
function currentRun() { return data.runs[state.currentIndex] || null; }
function runById(runId) { return data.runs.find(run => run.id === runId) || null; }
function reviewStatus(runId) {
  const run = runById(runId);
  return run ? derivedRunStatus(run) : state.status[runId] || 'unreviewed';
}
function reviewSeverity(runId) {
  const run = runById(runId);
  return run ? derivedRunSeverity(run) : state.severity[runId] || 'none';
}
function isBookmarked(runId) { return Boolean(state.bookmarks?.[runId]); }
function fileBookmarkKey(run, index = currentFileIndex(run)) {
  const file = run?.outputs?.[index];
  return run && file ? outputKey(run.id, file, index) : '';
}
function isFileBookmarked(key) { return Boolean(key && state.fileBookmarks?.[key]); }
function runHasFileBookmark(run) {
  return fileReviewEntries(run).some(({ key }) => isFileBookmarked(key));
}
function runHasAnyBookmark(run) {
  return isBookmarked(run.id) || runHasFileBookmark(run);
}
function clampFileIndex(run, index) {
  const max = Math.max(0, (run?.outputs || []).length - 1);
  const number = Number(index);
  return Number.isInteger(number) ? Math.min(Math.max(number, 0), max) : 0;
}
function currentFileIndex(run = currentRun()) {
  if (!run) return 0;
  if (!state.fileCursorByRunId) state.fileCursorByRunId = {};
  const next = clampFileIndex(run, state.fileCursorByRunId[run.id]);
  state.fileCursorByRunId[run.id] = next;
  return next;
}
function currentFileEntry(run = currentRun()) {
  const index = currentFileIndex(run);
  const file = run?.outputs?.[index];
  return file ? { index, file, key: outputKey(run.id, file, index), review: fileReviewForKey(outputKey(run.id, file, index)) } : null;
}
function setCurrentFileIndex(run, index) {
  if (!run) return;
  if (!state.fileCursorByRunId) state.fileCursorByRunId = {};
  state.fileCursorByRunId[run.id] = clampFileIndex(run, index);
}
function hasDecision(runId) {
  const run = runById(runId);
  return reviewStatus(runId) !== 'unreviewed' || (run && fileReviewSummary(run).reviewed > 0) || Boolean((state.feedback[runId] || '').trim());
}
function checklistFor(runId) {
  if (!state.checklist[runId]) state.checklist[runId] = { output_inspected: false, grades_checked: false, previous_output_checked: false, benchmark_checked: false, feedback_decision: false };
  if (state.checklist[runId].feedback_written != null && state.checklist[runId].feedback_decision == null) state.checklist[runId].feedback_decision = Boolean(state.checklist[runId].feedback_written);
  for (const [key] of CHECKLIST_ITEMS) if (state.checklist[runId][key] == null) state.checklist[runId][key] = false;
  return state.checklist[runId];
}
function fileReviewForKey(key) {
  if (!state.fileReviews) state.fileReviews = {};
  if (!state.fileReviews[key]) state.fileReviews[key] = { inspected: false, decision: 'unreviewed', severity: 'none', note: '' };
  if (!state.fileReviews[key].decision) state.fileReviews[key].decision = 'unreviewed';
  state.fileReviews[key].severity = normalizeSeverity(state.fileReviews[key].severity || severityForDecision(state.fileReviews[key].decision));
  if (state.fileReviews[key].note == null) state.fileReviews[key].note = '';
  state.fileReviews[key].inspected = Boolean(state.fileReviews[key].inspected);
  if (state.fileReviews[key].decision !== 'unreviewed') state.fileReviews[key].inspected = true;
  return state.fileReviews[key];
}
function fileReviewEntries(run) {
  return (run?.outputs || []).map((file, index) => {
    const key = outputKey(run.id, file, index);
    return { key, file, index, review: fileReviewForKey(key) };
  });
}
function fileReviewSummary(run) {
  const entries = fileReviewEntries(run);
  const reviewed = entries.filter(entry => entry.review.inspected && entry.review.decision !== 'unreviewed').length;
  const blocked = entries.filter(entry => entry.review.decision === 'blocked').length;
  const issue = entries.filter(entry => entry.review.decision === 'issue').length;
  const skipped = entries.filter(entry => entry.review.decision === 'skipped').length;
  return { total: entries.length, reviewed, missing: Math.max(0, entries.length - reviewed), blocked, issue, skipped, complete: entries.length === 0 || reviewed === entries.length };
}
function derivedRunStatus(run) {
  const summary = fileReviewSummary(run);
  if (!summary.reviewed) return 'unreviewed';
  if (summary.blocked) return 'blocked';
  if (summary.issue) return 'needs_changes';
  return summary.complete ? 'approved' : 'unreviewed';
}
function derivedRunSeverity(run) {
  const severities = fileReviewEntries(run)
    .filter(entry => entry.review.decision !== 'unreviewed')
    .map(entry => fileReviewSeverity(entry.review));
  const max = severities.reduce((best, value) => SEVERITY_RANK[value] > SEVERITY_RANK[best] ? value : best, 'none');
  if (max !== 'none') return max;
  const summary = fileReviewSummary(run);
  if (summary.blocked) return 'major';
  if (summary.issue) return 'minor';
  return 'none';
}
function severityForDecision(decision) {
  if (decision === 'blocked') return 'major';
  if (decision === 'issue') return 'minor';
  if (decision === 'skipped') return 'unknown';
  return 'none';
}
function normalizeSeverity(value) {
  return Object.prototype.hasOwnProperty.call(SEVERITY_RANK, value) ? value : 'none';
}
function fileReviewSeverity(review) {
  return normalizeSeverity(review?.severity || severityForDecision(review?.decision));
}
function fileReviewFeedback(run) {
  return fileReviewEntries(run)
    .filter(entry => entry.review.note?.trim())
    .map(entry => `${entry.file.name || 'output'}: ${entry.review.note.trim()}`)
    .join('\n');
}
function syncOutputChecklist(run) {
  if (!run) return;
  const summary = fileReviewSummary(run);
  const checklist = checklistFor(run.id);
  checklist.output_inspected = summary.complete;
  checklist.feedback_decision = summary.complete || Boolean(fileReviewFeedback(run).trim());
}
function isComplete(run) { return completionIssues(run, { includeQuality: true }).length === 0; }
function runNeedsFeedback(run) {
  const status = reviewStatus(run.id);
  return ['needs_changes', 'blocked'].includes(status) && !fileReviewFeedback(run).trim();
}
function hasPreviousContext(run) { return Boolean((data.previous_outputs || {})[run.id] || data.previous_feedback?.[run.id]); }
function gradeItems(run) { return run.grading?.expectations || run.grading?.assertions || []; }
function hasGrades(run) { return Boolean(run.grading?.summary || gradeItems(run).length); }
function failedGradeCount(run) { return gradeItems(run).filter(item => !item.passed).length; }
function hasBenchmarkForRun(run) {
  if (run.benchmark || run.benchmark_regression) return true;
  const byRun = data.benchmark?.by_run || data.benchmark?.byRun || {};
  if (byRun[run.id]) return true;
  const rows = data.benchmark?.runs || data.benchmark?.run_results || [];
  return Array.isArray(rows) && rows.some(row => row.run_id === run.id || row.id === run.id);
}
function requirementsForRun(run) {
  const requirements = [];
  requirements.push({ key: 'output_inspected', label: CHECKLIST_LABELS.output_inspected, reason: 'Every output file needs a file review.' });
  if (hasGrades(run)) requirements.push({ key: 'grades_checked', label: CHECKLIST_LABELS.grades_checked, reason: 'Formal grade data exists for this run.' });
  if (hasPreviousContext(run)) requirements.push({ key: 'previous_output_checked', label: CHECKLIST_LABELS.previous_output_checked, reason: 'Previous feedback or output exists for this run.' });
  if (hasBenchmarkForRun(run)) requirements.push({ key: 'benchmark_checked', label: CHECKLIST_LABELS.benchmark_checked, reason: 'Benchmark data maps to this run.' });
  requirements.push({ key: 'feedback_decision', label: CHECKLIST_LABELS.feedback_decision, reason: 'Every export row needs a reviewer decision.' });
  return requirements;
}
function preciseIssueCopy(issue) {
  if (!issue) return 'Ready';
  if (issue.type === 'feedback') return '1 note needed';
  if (issue.type === 'checklist:output_inspected') return issue.label || 'Files left';
  if (issue.checklist && CHECKLIST_LABELS[issue.checklist]) return `${CHECKLIST_LABELS[issue.checklist]} missing`;
  return issue.label || 'Action needed';
}
function nextBestAction(run, entry = currentFileEntry(run)) {
  if (!entry) return 'No files';
  const review = entry.review;
  if (!review.inspected) return 'Inspect file';
  if (review.decision === 'unreviewed') return 'Choose decision';
  if (['issue', 'blocked'].includes(review.decision) && !review.note.trim()) return 'Add note';
  const missing = requirementsForRun(run).find(req => !checklistFor(run.id)[req.key]);
  return missing ? 'Complete checks' : 'Review clean';
}
function hasChangedPreviousOutput(run) {
  const previous = (data.previous_outputs || {})[run.id] || [];
  if (!previous.length) return false;
  const diff = lineDiff(firstText(previous), firstText(run.outputs));
  return diff.added + diff.removed > 0;
}
function hasFailedGrades(run) {
  const expectations = run.grading?.expectations || run.grading?.assertions || [];
  return expectations.some(item => !item.passed);
}
function hasUnsupportedArtifacts(run) { return (run.outputs || []).some(file => !KNOWN_OUTPUT_TYPES.has(fileType(file))); }
function allOutputsExpanded(run) {
  const keys = (run?.outputs || []).map((file, index) => outputKey(run.id, file, index));
  return keys.length > 0 && keys.every(key => state.collapsedFiles[key] === false);
}
function runSearchText(run) {
  const outputs = (run.outputs || []).map(file => outputText(file)).join(' ');
  const previous = [data.previous_feedback?.[run.id], ...((data.previous_outputs || {})[run.id] || []).map(file => outputText(file))].join(' ');
  const grades = (run.grading?.expectations || run.grading?.assertions || []).map(item => `${item.text || ''} ${item.evidence || ''}`).join(' ');
  return [run.id, run.configuration, run.prompt, run.outputs.map(o => o.name).join(' '), outputs, previous, grades, state.feedback[run.id] || '', fileReviewFeedback(run)].join(' ').toLowerCase();
}
function visibleRuns() {
  const q = state.search.trim().toLowerCase();
  const rows = data.runs.map((run, index) => ({ run, index })).filter(({ run }) => {
    const status = reviewStatus(run.id);
    const severity = reviewSeverity(run.id);
    const flow = state.flowFilter || (state.statusFilter === 'open' || state.statusFilter === 'complete' ? state.statusFilter : 'all');
    const flowMatches = flow === 'all' || (flow === 'open' && !isComplete(run)) || (flow === 'complete' && isComplete(run));
    const statusMatches = state.statusFilter === 'all' || state.statusFilter === status;
    const severityMatches = state.severityFilter === 'all' || state.severityFilter === severity;
    const bookmarkMatches = !state.bookmarkedOnly || runHasAnyBookmark(run);
    return (!q || runSearchText(run).includes(q)) && flowMatches && statusMatches && severityMatches && bookmarkMatches;
  });
  const direction = state.sortDirection === 'desc' ? -1 : 1;
  return rows.sort((a, b) => (a.index - b.index) * direction);
}
function stats() {
  const total = data.runs.length;
  const complete = data.runs.filter(r => isComplete(r)).length;
  const decided = data.runs.filter(r => hasDecision(r.id)).length;
  const blocked = data.runs.filter(r => reviewStatus(r.id) === 'blocked').length;
  const needs = data.runs.filter(r => reviewStatus(r.id) === 'needs_changes').length;
  return { total, complete, decided, remaining: total - complete, blocked, needs };
}
function render() {
  document.body.classList.toggle('theme-dark', state.theme === 'dark');
  markVisited();
  renderChrome();
  renderMetrics();
  renderFilterControl();
  renderRunList();
  renderTabs();
  renderReaderHead();
  renderMain();
  renderReviewDock();
  renderMobileBar();
  document.getElementById('search').value = state.search;
  persist();
}
function renderChrome() {
  const s = stats();
  const pct = s.total ? Math.round((s.complete / s.total) * 100) : 0;
  document.getElementById('skill-name').textContent = data.skill_name || 'Eval Review';
  document.getElementById('progress-label').textContent = `${s.complete} of ${s.total} complete / ${s.decided} decisions`;
  document.getElementById('progress-fill').style.width = `${pct}%`;
  const themeButton = document.getElementById('btn-theme');
  const nextTheme = state.theme === 'dark' ? 'Light profile' : 'Dark profile';
  themeButton.innerHTML = state.theme === 'dark' ? iconSun() : iconMoon();
  themeButton.setAttribute('aria-label', nextTheme);
  themeButton.dataset.tooltip = nextTheme;
  document.querySelector('.action-menu summary').innerHTML = iconMenu();
  document.getElementById('btn-close-submit').innerHTML = iconCross();
  document.getElementById('btn-close-bulk').innerHTML = iconCross();
  document.getElementById('btn-close-mobile-sheet').innerHTML = iconCross();
}
function renderMetrics() {
}
function renderFilterControl() {
  const el = document.getElementById('filter-control');
  if (!el) return;
  const flow = state.flowFilter || (state.statusFilter === 'open' || state.statusFilter === 'complete' ? state.statusFilter : 'all');
  const statusFilter = state.statusFilter === 'open' || state.statusFilter === 'complete' ? 'all' : state.statusFilter || 'all';
  const flowActive = flow === 'open' || flow === 'complete';
  const flowLabel = flow === 'complete' ? 'Closed' : flow === 'open' ? 'Open' : 'Open/Closed';
  const flowIcon = flow === 'complete' ? iconClosedRuns() : iconOpenRuns();
  const flowTooltip = flow === 'complete' ? 'Closed runs' : 'Open runs';
  el.innerHTML = `<div class="filter-strip" role="group" aria-label="Run filters">
    <div class="filter-icon-row" aria-label="Quick filters">
      <button type="button" class="filter-toggle${flowActive ? ' is-active' : ''}" data-flow-toggle aria-pressed="${flowActive ? 'true' : 'false'}" aria-label="${attr(flowTooltip)}" data-tooltip="${attr(flowTooltip)}">${flowIcon}<span>${escapeHtml(flowLabel)}</span></button>
      <button type="button" class="filter-toggle${state.bookmarkedOnly ? ' is-active' : ''}" data-filter-bookmarked aria-pressed="${state.bookmarkedOnly ? 'true' : 'false'}" aria-label="Show bookmarked runs only" data-tooltip="Bookmarked only">${state.bookmarkedOnly ? iconBookmarkSolid() : iconBookmark()}<span>Later</span></button>
      <button type="button" class="filter-toggle" data-random-run aria-label="Random incomplete run" data-tooltip="Random run">${iconShuffle()}<span>Random run</span></button>
      <button type="button" class="filter-toggle" data-clear-filters aria-label="Clear filters" data-tooltip="Clear filters">${iconCross()}<span>Clear filters</span></button>
      <button type="button" class="filter-toggle" data-sort-direction aria-pressed="${state.sortDirection === 'desc' ? 'true' : 'false'}" aria-label="${state.sortDirection === 'desc' ? 'Sort descending' : 'Sort ascending'}" data-tooltip="${state.sortDirection === 'desc' ? 'Descending' : 'Ascending'}">${state.sortDirection === 'desc' ? iconArrowDown() : iconArrowUp()}<span>${state.sortDirection === 'desc' ? 'Descending' : 'Ascending'}</span></button>
      <button type="button" id="btn-bulk-approve" class="filter-toggle" aria-label="Bulk approve visible" data-tooltip="Bulk approve visible">${iconCheck()}<span>Bulk approve visible</span></button>
    </div>
    <div class="filter-field"><span>Status</span>${renderDropdown('filter-status', STATUS_FILTER_OPTIONS, statusFilter, { tooltip: 'Filter by status' })}</div>
    <div class="filter-field"><span>Severity</span>${renderDropdown('filter-severity', SEVERITY_FILTER_OPTIONS, state.severityFilter || 'all', { tooltip: 'Filter by severity' })}</div>
  </div>`;
}
function renderRunList() {
  const runs = visibleRuns();
  if (!data.runs.length) {
    document.getElementById('run-list').innerHTML = renderSkeletonState('list');
    return;
  }
  document.getElementById('run-list').innerHTML = runs.length ? runs.map(({ run, index }) => {
    const active = index === state.currentIndex ? ' is-active' : '';
    const status = reviewStatus(run.id);
    const severity = reviewSeverity(run.id);
    const bookmarked = isBookmarked(run.id);
    const fileBookmarked = runHasFileBookmark(run);
    const meta = [bookmarked ? 'Run bookmarked' : '', fileBookmarked ? 'File bookmarked' : '', hasChangedPreviousOutput(run) ? 'Changed previous output' : '', labelForSeverity(severity), `${run.outputs.length} file${run.outputs.length === 1 ? '' : 's'}`].filter(Boolean).join(' / ');
    const promptPreview = run.prompt || 'No prompt captured.';
    const bookmarkLabel = bookmarked || fileBookmarked ? `<span class="run-bookmark">${iconBookmarkSolid()}<span>${bookmarked ? 'Run' : 'File'}</span></span>` : '';
    return `<button class="run-card${active}${bookmarked || fileBookmarked ? ' is-bookmarked' : ''}" data-status="${attr(status)}" data-run-index="${index}" role="option" aria-selected="${active ? 'true' : 'false'}" aria-label="${attr(`${run.id}, ${bookmarked || fileBookmarked ? 'bookmarked, ' : ''}${labelForStatus(status)}, ${meta}`)}">
      <div class="run-card-title"><strong>${escapeHtml(run.id)}</strong><span class="run-card-state">${bookmarkLabel}${escapeHtml(labelForStatus(status))}</span></div>
      <p class="run-card-prompt">${escapeHtml(promptPreview)}</p>
      <div class="run-card-meta">${escapeHtml(meta)}</div>
    </button>`;
  }).join('') : '<div class="empty">No runs match this view.</div>';
}
function renderTabs() {
  document.querySelectorAll('#tabs button').forEach(btn => {
    const active = btn.dataset.tab === state.tab;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    btn.setAttribute('aria-controls', 'main-content');
    btn.id = `${btn.dataset.tab}-tab`;
  });
}
function renderReaderHead() {
  const run = currentRun();
  const title = document.getElementById('run-title');
  if (run) {
    const bookmarked = isBookmarked(run.id);
    title.innerHTML = `<button id="btn-run-bookmark" class="title-bookmark" aria-label="${bookmarked ? 'Remove run bookmark' : 'Bookmark run'}" aria-pressed="${bookmarked ? 'true' : 'false'}" data-tooltip="${bookmarked ? 'Run bookmarked' : 'Bookmark run'}">${bookmarked ? iconBookmarkSolid() : iconBookmark()}</button><span>${escapeHtml(run.id)}</span>`;
  } else {
    title.textContent = 'No run selected';
  }
  document.getElementById('run-summary').textContent = run ? (run.prompt || 'No prompt captured.') : '';
}
function renderMain() {
  if (state.tab === 'benchmark') return renderBenchmark();
  if (state.tab === 'feedback') return renderFeedbackPreview();
  return renderReview();
}
function renderReview() {
  const run = currentRun();
  const main = document.getElementById('main-content');
  if (!run) { main.innerHTML = `<div class="reading-column">${renderSkeletonState('reader')}</div>`; return; }
  const entry = currentFileEntry(run);
  main.setAttribute('aria-labelledby', 'review-tab');
  main.innerHTML = `<div class="reading-column">
    <section class="reader-section" id="prompt-section"><div class="section-label">Prompt</div><div class="prompt-text">${escapeHtml(run.prompt || 'No prompt captured.')}</div></section>
    <section class="reader-section" id="output-section" tabindex="-1"><div class="section-header-line"><div class="section-label">Output</div></div>${renderReveal(run)}<div class="artifact-list">${renderOutputs(run.outputs, run.id)}</div></section>
    <section class="reader-section" id="grades-section" tabindex="-1"><div class="section-label">File Grading</div>${renderFileGrades(run, entry)}</section>
    <section class="reader-section" id="previous-context-section" tabindex="-1"><div class="section-label">Previous</div>${renderPreviousContext(run, entry)}</section>
  </div>`;
}
function renderOutputs(outputs, runId) {
  if (!outputs.length) return '<div class="empty">No output files found.</div>';
  const run = runById(runId);
  const index = currentFileIndex(run);
  const file = outputs[index] || outputs[0];
  const key = outputKey(runId, file, index);
  const collapsed = state.collapsedFiles[key] !== false;
  const actions = renderArtifactActions(file, key);
  const fileName = file.name || 'output';
  return `<article class="artifact${collapsed ? ' is-collapsed' : ''}" data-output-key="${attr(key)}"><header><div class="artifact-title"><button class="file-toggle" data-file-toggle="${attr(key)}" aria-expanded="${collapsed ? 'false' : 'true'}" aria-label="${collapsed ? 'Expand' : 'Collapse'} ${attr(fileName)}">${iconChevronDown()}</button><strong>${escapeHtml(fileName)}</strong>${renderFileBadges(file)}</div>${actions ? `<div class="artifact-actions">${actions}</div>` : ''}</header>${collapsed ? '' : renderOutputContent(file, key)}</article>`;
}
function renderFileNavigator(run, scope) {
  const entry = currentFileEntry(run);
  const total = (run?.outputs || []).length;
  if (!entry || total <= 0) return '<div class="section-actions"></div>';
  const canPrev = entry.index > 0;
  const canNext = entry.index < total - 1;
  const fileBookmarked = isFileBookmarked(entry.key);
  if (scope === 'dock') {
    return `<div class="file-nav file-nav-dock" data-scope="${attr(scope)}">
      <button class="icon-link" data-file-nav="prev" aria-label="Previous file" data-tooltip="Previous file" ${canPrev ? '' : 'disabled'}>${iconArrowLeft()}</button>
      <span class="file-nav-count">${entry.index + 1} of ${total}</span>
      <button class="icon-link${fileBookmarked ? ' is-active' : ''}" data-file-bookmark aria-label="${fileBookmarked ? 'Remove file bookmark' : 'Bookmark file'}" aria-pressed="${fileBookmarked ? 'true' : 'false'}" data-tooltip="${fileBookmarked ? 'File bookmarked' : 'Bookmark file'}">${fileBookmarked ? iconBookmarkSolid() : iconBookmark()}</button>
      <button class="icon-link" data-random-file aria-label="Random unfinished file" data-tooltip="Random file">${iconShuffle()}</button>
      <button class="icon-link" data-file-nav="next" aria-label="Next file" data-tooltip="Next file" ${canNext ? '' : 'disabled'}>${iconArrowRight()}</button>
    </div>`;
  }
  return '<div class="section-actions"></div>';
}
function renderReveal(run) {
  if (!revealState || revealState.runId !== run?.id) return '';
  return `<div class="reveal-note" data-reveal="${attr(revealState.type || 'file')}">${escapeHtml(revealState.message)}</div>`;
}
function renderArtifactActions(file, key) {
  const actions = [];
  if (canCopyFile(file)) actions.push(`<button class="icon-link" data-copy-output="${attr(key)}" aria-label="Copy ${attr(file.name || 'output')}" data-tooltip="Copy">${iconCopy()}</button>`);
  if (canOpenFile(file)) actions.push(`<button class="icon-command" data-open-output="${attr(key)}" aria-label="Open ${attr(file.name || 'output')}" data-tooltip="Open">${iconOpen()}</button>`);
  if (canDownloadFile(file)) actions.push(downloadIconLink(file));
  return actions.join('');
}
function downloadIconLink(file) {
  const name = file.name || 'output.txt';
  return `<a class="icon-link" href="${attr(downloadUri(file))}" download="${attr(name)}" aria-label="Download ${attr(name)}" data-tooltip="Download">${iconDownload()}</a>`;
}
function iconChevronDown() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="m6 9 6 6 6-6"></path></svg>';
}
function iconChevronRight() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon summary-chevron"><path d="m9 18 6-6-6-6"></path></svg>';
}
function iconExpandAll() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><rect x="4" y="4" width="16" height="6" rx="1"></rect><rect x="4" y="14" width="16" height="6" rx="1"></rect><path d="M12 5.8v2.4"></path><path d="M10.8 7h2.4"></path><path d="M12 15.8v2.4"></path><path d="M10.8 17h2.4"></path></svg>';
}
function iconCollapseAll() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><rect x="4" y="4" width="16" height="6" rx="1"></rect><rect x="4" y="14" width="16" height="6" rx="1"></rect><path d="M10.8 7h2.4"></path><path d="M10.8 17h2.4"></path></svg>';
}
function iconDownload() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path></svg>';
}
function iconCopy() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
}
function iconOpen() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M15 3h6v6"></path><path d="M10 14 21 3"></path><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"></path></svg>';
}
function iconUpload() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M12 21V9"></path><path d="m7 14 5-5 5 5"></path><path d="M5 3h14"></path></svg>';
}
function iconMenu() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M4 7h16"></path><path d="M4 12h16"></path><path d="M4 17h16"></path></svg>';
}
function iconRows() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M4 6h16"></path><path d="M4 12h16"></path><path d="M4 18h16"></path></svg>';
}
function iconColumns() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><rect x="4" y="4" width="7" height="16" rx="1"></rect><rect x="13" y="4" width="7" height="16" rx="1"></rect></svg>';
}
function iconOpenRuns() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><circle cx="12" cy="12" r="8"></circle><path d="M8 12h8"></path></svg>';
}
function iconClosedRuns() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><circle cx="12" cy="12" r="8"></circle><path d="m9 12 2 2 4-5"></path></svg>';
}
function iconArrowUp() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="m12 5-7 7"></path><path d="m12 5 7 7"></path><path d="M12 19V5"></path></svg>';
}
function iconArrowDown() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M12 5v14"></path><path d="m19 12-7 7-7-7"></path></svg>';
}
function iconArrowLeft() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="m12 19-7-7 7-7"></path><path d="M19 12H5"></path></svg>';
}
function iconArrowRight() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M5 12h14"></path><path d="m12 5 7 7-7 7"></path></svg>';
}
function iconBookmark() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M19 21 12 17 5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2Z"></path></svg>';
}
function iconBookmarkSolid() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon is-filled"><path d="M19 21 12 17 5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2Z"></path></svg>';
}
function iconShuffle() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M16 3h5v5"></path><path d="m21 3-7 7"></path><path d="M4 20l6-6"></path><path d="M4 4h3c2 0 3.2 1.2 4.5 3.5"></path><path d="M21 21h-5v-5"></path><path d="m15 15 6 6"></path><path d="M4 20h3c2 0 3.2-1.2 4.5-3.5"></path></svg>';
}
function iconCheck() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="m20 6-11 11-5-5"></path></svg>';
}
function iconTrash() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="m19 6-1 14H6L5 6"></path><path d="M10 11v5"></path><path d="M14 11v5"></path></svg>';
}
function iconSave() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"></path><path d="M17 21v-8H7v8"></path><path d="M7 3v5h8"></path></svg>';
}
function iconCornerDownLeft() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M9 10 4 15l5 5"></path><path d="M20 4v7a4 4 0 0 1-4 4H4"></path></svg>';
}
function iconMoon() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M20.4 15.7A8 8 0 0 1 8.3 3.6 9 9 0 1 0 20.4 15.7Z"></path></svg>';
}
function iconSun() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2"></path><path d="M12 20v2"></path><path d="m4.93 4.93 1.41 1.41"></path><path d="m17.66 17.66 1.41 1.41"></path><path d="M2 12h2"></path><path d="M20 12h2"></path><path d="m6.34 17.66-1.41 1.41"></path><path d="m19.07 4.93-1.41 1.41"></path></svg>';
}
function iconCheck() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M20 6 9 17l-5-5"></path></svg>';
}
function iconCross() {
  return '<svg viewBox="0 0 24 24" aria-hidden="true" class="status-icon"><path d="M18 6 6 18M6 6l12 12"></path></svg>';
}
function renderOutputContent(file, key) {
  if (file.type === 'image' && file.data_uri) return `<img src="${attr(file.data_uri)}" alt="${attr(file.name || 'image output')}">`;
  if (file.type === 'pdf' && file.data_uri) return `<iframe src="${attr(file.data_uri)}" title="${attr(file.name || 'PDF output')}"></iframe>`;
  if (file.type === 'json') return renderTextPreview(JSON.stringify(file.content, null, 2), key, 'json');
  if (file.type === 'csv') return renderDelimitedTable(file.content || '', ',', file.name || 'csv');
  if (file.type === 'tsv') return renderDelimitedTable(file.content || '', '\t', file.name || 'tsv');
  if (file.type === 'xlsx') return renderXlsxPreview(file);
  if (file.type === 'binary') return canDownloadFile(file) ? `<div class="artifact-body">${downloadIconLink(file)}</div>` : '<div class="artifact-body"><div class="empty">No embedded binary data.</div></div>';
  return renderTextPreview(file.content || '', key, 'text');
}
function renderTextPreview(content, key, tone) {
  const text = String(content || '');
  const expanded = Boolean(state.expandedOutputs[key]);
  const isLong = text.length > TEXT_PREVIEW_CHARS;
  const shown = expanded || !isLong ? text : text.slice(0, TEXT_PREVIEW_CHARS);
  const cls = tone === 'json' ? 'json-preview' : 'code-preview';
  const notice = isLong ? `<div class="preview-note">Showing ${formatBytes(byteLength(shown))} of ${formatBytes(byteLength(text))}.<button data-output-expand="${attr(key)}">${expanded ? 'Collapse' : 'Show full'}</button></div>` : '';
  return `<pre class="${cls}">${escapeHtml(shown).slice(0, MAX_PREVIEW)}</pre>${notice}`;
}
function renderDelimitedTable(text, delimiter, label) {
  const parsed = parseDelimitedRows(String(text || ''), delimiter);
  if (!parsed.rows.length) return '<div class="artifact-body"><div class="empty">No tabular rows found.</div></div>';
  const [header, ...body] = parsed.rows;
  return `<div class="table-wrap"><table class="data-table" aria-label="${attr(label)} preview"><thead><tr>${header.map(cell => `<th>${escapeHtml(cell)}</th>`).join('')}</tr></thead><tbody>${body.map(row => `<tr>${header.map((_, i) => `<td>${escapeHtml(row[i] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${parsed.truncated ? '<div class="preview-note">Preview truncated. Download the artifact for full contents.</div>' : ''}`;
}
function parseDelimitedRows(text, delimiter) {
  const rows = [];
  let row = [], cell = '', quoted = false;
  const source = text.slice(0, 300000);
  for (let i = 0; i < source.length; i++) {
    const ch = source[i];
    if (ch === '"') {
      if (quoted && source[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted;
    } else if (ch === delimiter && !quoted) { row.push(cell); cell = ''; }
    else if (ch === '\n' && !quoted) {
      row.push(cell.replace(/\r$/, ''));
      if (row.some(v => v.trim())) rows.push(row.slice(0, TABLE_PREVIEW_COLS));
      row = []; cell = '';
      if (rows.length > TABLE_PREVIEW_ROWS) break;
    } else cell += ch;
  }
  if (row.length || cell) {
    row.push(cell.replace(/\r$/, ''));
    if (row.some(v => v.trim())) rows.push(row.slice(0, TABLE_PREVIEW_COLS));
  }
  return { rows: rows.slice(0, TABLE_PREVIEW_ROWS + 1), truncated: rows.length > TABLE_PREVIEW_ROWS || text.length > source.length };
}
function renderXlsxPreview(file) {
  const sheets = workbookSheets(file);
  if (!sheets.length) return canDownloadFile(file) ? `<div class="artifact-body">${downloadIconLink(file)}<div class="preview-note">No workbook preview rows supplied.</div></div>` : '<div class="artifact-body"><div class="empty">No workbook preview rows supplied.</div></div>';
  return sheets.slice(0, 2).map(sheet => {
    const rows = (sheet.rows || []).slice(0, TABLE_PREVIEW_ROWS + 1);
    if (!rows.length) return `<div class="artifact-body"><div class="empty">${escapeHtml(sheet.name || 'Sheet')} has no preview rows.</div></div>`;
    const [header, ...body] = rows;
    return `<div class="preview-note"><strong>${escapeHtml(sheet.name || 'Sheet')}</strong></div><div class="table-wrap"><table class="data-table" aria-label="${attr(sheet.name || 'Workbook sheet')} preview"><thead><tr>${header.map(cell => `<th>${escapeHtml(cell)}</th>`).join('')}</tr></thead><tbody>${body.map(row => `<tr>${header.map((_, i) => `<td>${escapeHtml(row[i] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${(sheet.rows || []).length > rows.length ? '<div class="preview-note">Sheet preview truncated.</div>' : ''}`;
  }).join('');
}
function workbookSheets(file) {
  if (Array.isArray(file.sheets)) return file.sheets;
  if (Array.isArray(file.preview)) return [{ name: 'Preview', rows: file.preview }];
  if (Array.isArray(file.content?.sheets)) return file.content.sheets;
  if (Array.isArray(file.content?.preview)) return [{ name: 'Preview', rows: file.content.preview }];
  return [];
}
function renderPreviousContext(run, entry = currentFileEntry(run)) {
  return `<div class="artifact-list">${renderPreviousFeedback(run, entry)}${renderPreviousOutputDiff(run, entry)}</div>`;
}
function fileRefMatchesEntry(value, entry) {
  if (!entry || value == null) return false;
  const fileName = String(entry.file?.name || '').toLowerCase();
  const normalized = String(value).toLowerCase();
  return normalized === fileName || normalized.endsWith(`/${fileName}`) || normalized.endsWith(`\\${fileName}`) || normalized.includes(fileName);
}
function previousFeedbackForEntry(run, entry) {
  const source = data.previous_feedback?.[run.id];
  if (!source || !entry) return '';
  const fileName = entry.file?.name || '';
  if (typeof source === 'object' && !Array.isArray(source)) {
    const direct = source[fileName] ?? source[String(entry.index)] ?? source[String(entry.index + 1)];
    if (typeof direct === 'string') return direct;
    const files = source.files || source.outputs || source.feedback;
    if (Array.isArray(files)) {
      const match = files.find(item => ['file', 'file_name', 'filename', 'path', 'output', 'output_name'].some(key => fileRefMatchesEntry(item?.[key], entry)));
      return String(match?.feedback || match?.note || match?.text || '');
    }
  }
  if (Array.isArray(source)) {
    const match = source.find(item => ['file', 'file_name', 'filename', 'path', 'output', 'output_name'].some(key => fileRefMatchesEntry(item?.[key], entry)));
    return String(match?.feedback || match?.note || match?.text || '');
  }
  return '';
}
function renderPreviousFeedback(run, entry = currentFileEntry(run)) {
  const prevFeedback = previousFeedbackForEntry(run, entry);
  const fileName = entry?.file?.name || 'this file';
  return prevFeedback ? `<article class="previous-note"><div class="previous-note-label">Previous feedback for ${escapeHtml(fileName)}</div><p>${escapeHtml(prevFeedback)}</p></article>` : `<div class="empty">No previous feedback for ${escapeHtml(fileName)}.</div>`;
}
function previousOutputForEntry(run, entry) {
  const prev = (data.previous_outputs || {})[run.id] || [];
  if (!entry || !prev.length) return null;
  const currentName = String(entry.file?.name || '').toLowerCase();
  return prev.find(file => String(file.name || '').toLowerCase() === currentName) || prev[entry.index] || null;
}
function renderPreviousOutputDiff(run, entry = currentFileEntry(run)) {
  const prev = (data.previous_outputs || {})[run.id] || [];
  if (!prev.length) return '<div class="empty">No previous output for this run.</div>';
  const current = entry?.file || run.outputs?.[currentFileIndex(run)];
  const previous = previousOutputForEntry(run, entry);
  if (!previous) return `<div class="empty">No previous output for ${escapeHtml(current?.name || 'this file')}.</div>`;
  const left = outputText(previous);
  const right = outputText(current);
  const diff = lineDiff(left, right);
  return `<div class="comparison-block"><div class="section-label comparison-label">Comparison</div><article class="artifact diff-artifact"><header><div class="artifact-title"><span class="pill">${diff.changed} changed</span><span class="pill" data-tone="warn">${diff.removed} removed</span><span class="pill" data-tone="accent">${diff.added} added</span></div><div class="segmented-icons" aria-label="Comparison mode"><button class="icon-link${state.comparisonMode === 'unified' ? ' is-active' : ''}" data-diff-mode="unified" aria-label="Unified diff" data-tooltip="Unified">${iconRows()}</button><button class="icon-link${state.comparisonMode === 'side' ? ' is-active' : ''}" data-diff-mode="side" aria-label="Side by side comparison" data-tooltip="Side by side">${iconColumns()}</button></div></header>${state.comparisonMode === 'side' ? renderSideBySide(left, right) : renderDiffPreview(diff)}</article></div>`;
}
function firstText(outputs) {
  const file = outputs.find(o => o.type === 'text' || o.type === 'json') || outputs[0];
  if (!file) return '';
  return file.type === 'json' ? JSON.stringify(file.content, null, 2) : String(file.content || '');
}
function lineDiff(leftText, rightText) {
  const left = String(leftText || '').split(/\r?\n/).slice(0, 600);
  const right = String(rightText || '').split(/\r?\n/).slice(0, 600);
  const dp = Array.from({ length: left.length + 1 }, () => Array(right.length + 1).fill(0));
  for (let i = left.length - 1; i >= 0; i--) {
    for (let j = right.length - 1; j >= 0; j--) {
      dp[i][j] = left[i] === right[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0, added = 0, removed = 0;
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) {
      ops.push({ type: 'context', oldLine: i + 1, newLine: j + 1, text: left[i] });
      i++; j++;
    } else if (j < right.length && (i === left.length || dp[i][j + 1] >= (dp[i + 1]?.[j] ?? 0))) {
      ops.push({ type: 'add', oldLine: '', newLine: j + 1, text: right[j++] });
      added++;
    } else if (i < left.length) {
      ops.push({ type: 'remove', oldLine: i + 1, newLine: '', text: left[i++] });
      removed++;
    }
  }
  const rows = compactDiffRows(ops, 2, 240);
  return { changed: Math.max(added, removed), added, removed, rows };
}
function compactDiffRows(ops, radius, limit) {
  const changed = ops.map((op, index) => op.type !== 'context' ? index : -1).filter(index => index >= 0);
  if (!changed.length) return [];
  const keep = new Set();
  for (const index of changed) {
    for (let i = Math.max(0, index - radius); i <= Math.min(ops.length - 1, index + radius); i++) keep.add(i);
  }
  const rows = [];
  let omitted = 0;
  for (let index = 0; index < ops.length && rows.length < limit; index++) {
    if (!keep.has(index)) { omitted++; continue; }
    if (omitted) {
      rows.push({ type: 'gap', oldLine: '', newLine: '', text: `${omitted} unchanged line${omitted === 1 ? '' : 's'} hidden` });
      omitted = 0;
      if (rows.length >= limit) break;
    }
    rows.push(ops[index]);
  }
  return rows;
}
function renderDiffPreview(diff) {
  if (!diff.rows.length) return '<div class="diff-preview"><div class="diff-empty">No line changes.</div></div>';
  return `<div class="diff-preview" role="table" aria-label="Line diff">${diff.rows.map(row => {
    const sign = row.type === 'add' ? '+' : row.type === 'remove' ? '-' : '';
    const line = row.type === 'add' ? row.newLine : row.oldLine || row.newLine;
    return `<div class="diff-row diff-${row.type}" role="row"><span class="diff-marker" aria-hidden="true"></span><span class="diff-line">${escapeHtml(line)}</span><span class="diff-sign" aria-hidden="true">${sign}</span><code>${escapeHtml(row.text)}</code></div>`;
  }).join('')}</div>`;
}
function renderSideBySide(leftText, rightText) {
  return `<div class="side-by-side"><div><div class="preview-note">Previous</div><pre class="code-preview">${escapeHtml(String(leftText || '')).slice(0, MAX_PREVIEW)}</pre></div><div><div class="preview-note">Current</div><pre class="code-preview">${escapeHtml(String(rightText || '')).slice(0, MAX_PREVIEW)}</pre></div></div>`;
}
function gradeItemHasFileScope(item) {
  return ['file', 'file_name', 'filename', 'file_path', 'output', 'output_name', 'path', 'artifact', 'output_index', 'file_index', 'outputIndex', 'fileIndex'].some(key => item?.[key] != null);
}
function gradeItemMatchesEntry(item, entry) {
  if (!entry) return false;
  const fileName = String(entry.file?.name || '').toLowerCase();
  const fields = ['file', 'file_name', 'filename', 'file_path', 'output', 'output_name', 'path', 'artifact']
    .map(key => item?.[key])
    .filter(value => value != null)
    .map(value => String(value).toLowerCase());
  const indexFields = [item?.output_index, item?.file_index, item?.outputIndex, item?.fileIndex].filter(value => value != null);
  const nameMatch = fields.some(value => value === fileName || value.endsWith(`/${fileName}`) || value.endsWith(`\\${fileName}`) || value.includes(fileName));
  const indexMatch = indexFields.some(value => Number(value) === entry.index || Number(value) === entry.index + 1 || String(value).trim() === String(entry.index + 1));
  return nameMatch || indexMatch;
}
function gradeSummaryFromItems(expectations) {
  if (!expectations.length) return null;
  const passed = expectations.filter(e => e.passed).length;
  return { rate: Math.round((passed / expectations.length) * 100), passed, total: expectations.length };
}
function renderGradeRows(expectations) {
  return expectations.length ? `<div class="grade-list">${expectations.map(item => {
    const passed = Boolean(item.passed);
    return `<div class="grade-row"><span class="grade-mark ${passed ? 'pass' : 'fail'}" aria-label="${passed ? 'Pass' : 'Fail'}">${passed ? iconCheck() : iconCross()}</span><div><strong>${escapeHtml(item.text || item.name || 'Expectation')}</strong>${item.evidence ? `<div class="evidence">${escapeHtml(item.evidence)}</div>` : ''}</div></div>`;
  }).join('')}</div>` : '';
}
function renderGradeSummary(summary) {
  return summary ? `<div class="pill-row"><span class="pill" data-tone="${summary.rate >= 80 ? 'good' : summary.rate >= 50 ? 'warn' : 'bad'}">${summary.rate}% pass</span><span class="pill">${summary.passed}/${summary.total}</span></div>` : '';
}
function renderFileGrades(run, entry = currentFileEntry(run)) {
  const allExpectations = run.grading?.expectations || run.grading?.assertions || [];
  const expectations = allExpectations.filter(item => gradeItemHasFileScope(item) && gradeItemMatchesEntry(item, entry));
  const g = gradeSummaryFromItems(expectations);
  const fileName = entry?.file?.name || 'this file';
  if (!g && !expectations.length) return `<div class="empty">No file grades supplied for ${escapeHtml(fileName)}. Run formal grades are on Benchmark.</div>`;
  return `${renderGradeSummary(g)}${renderGradeRows(expectations)}`;
}
function renderRunFormalGrades(run) {
  const expectations = run.grading?.expectations || run.grading?.assertions || [];
  const g = gradeSummary(run);
  if (!g && !expectations.length) return '<div class="empty">No formal grades supplied for this run.</div>';
  return `${renderGradeSummary(g)}${renderGradeRows(expectations)}`;
}
function gradeSummary(run) {
  const s = run.grading?.summary;
  if (s) {
    const total = Number(s.total ?? s.count ?? 0);
    const passed = Number(s.passed ?? Math.round((s.pass_rate || 0) * total));
    const rate = s.pass_rate != null ? Math.round(Number(s.pass_rate) * 100) : total ? Math.round((passed / total) * 100) : null;
    return rate == null ? null : { rate, passed, total };
  }
  const expectations = run.grading?.expectations || run.grading?.assertions || [];
  if (expectations.length) {
    const passed = expectations.filter(e => e.passed).length;
    return { rate: Math.round((passed / expectations.length) * 100), passed, total: expectations.length };
  }
  return null;
}
function renderBenchmark() {
  const b = data.benchmark;
  const run = currentRun();
  const main = document.getElementById('main-content');
  main.setAttribute('aria-labelledby', 'benchmark-tab');
  const sections = [];
  if (b) {
    let rows;
    if (b.variants) {
      const assisted = b.variants.with_skill || {};
      const baseline = b.variants.baseline || {};
      const delta = b.delta_with_skill_minus_baseline || {};
      rows = [
        ['Pass rate', metricMean(assisted.pass_rate), metricMean(baseline.pass_rate), metricMean(delta.pass_rate)],
        ['Mean score', metricWithSpread(assisted.score_mean, assisted.score_stdev), metricWithSpread(baseline.score_mean, baseline.score_stdev), metricMean(delta.score_mean)],
        ['Mean seconds', metricMean(assisted.duration_seconds_mean), metricMean(baseline.duration_seconds_mean), metricMean(delta.duration_seconds_mean)],
        ['Mean tokens', metricMean(assisted.tokens_mean), metricMean(baseline.tokens_mean), metricMean(delta.tokens_mean)]
      ];
    } else {
      const summary = b.run_summary || {};
      rows = [
        ['Pass rate', metricMean(summary.with_skill?.pass_rate), metricMean(summary.without_skill?.pass_rate), metricMean(summary.delta?.pass_rate)],
        ['Score', metricMean(summary.with_skill?.score), metricMean(summary.without_skill?.score), metricMean(summary.delta?.score)]
      ];
    }
    sections.push(`<section class="reader-section"><div class="section-label">Benchmark Summary</div><article class="artifact"><div class="table-wrap"><table class="data-table"><thead><tr><th>Metric</th><th>With skill</th><th>Without skill</th><th>Delta</th></tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${escapeHtml(cell ?? 'n/a')}</td>`).join('')}</tr>`).join('')}</tbody></table></div></article></section>`);
    if (b.scenarios && typeof b.scenarios === 'object') {
      const scenarioRows = Object.entries(b.scenarios).map(([id, item]) => {
        const baseline = item?.baseline || {};
        const assisted = item?.with_skill || {};
        const delta = item?.delta || {};
        return `<tr><td>${escapeHtml(id)}</td><td>${escapeHtml(metricMean(baseline.pass_rate) ?? 'n/a')}</td><td>${escapeHtml(metricMean(assisted.pass_rate) ?? 'n/a')}</td><td>${escapeHtml(metricMean(delta.pass_rate) ?? 'n/a')}</td><td>${escapeHtml(metricMean(delta.score_mean) ?? 'n/a')}</td></tr>`;
      }).join('');
      sections.push(`<section class="reader-section"><div class="section-label">Scenario Results</div><article class="artifact"><div class="table-wrap"><table class="data-table"><thead><tr><th>Scenario</th><th>Baseline pass</th><th>Skill pass</th><th>Pass delta</th><th>Score delta</th></tr></thead><tbody>${scenarioRows || '<tr><td colspan="5">No scenario aggregates.</td></tr>'}</tbody></table></div></article></section>`);
    }
    if (b.pairing) {
      const pairedRows = Object.entries(b.pairing.paired_delta_summary || {}).map(([metric, item]) =>
        `<tr><td>${escapeHtml(metric)}</td><td>${escapeHtml(item?.pairs ?? 0)}</td><td>${escapeHtml(metricMean(item?.mean) ?? 'n/a')}</td><td>${escapeHtml(metricMean(item?.stdev) ?? 'n/a')}</td></tr>`
      ).join('');
      const unmatched = Array.isArray(b.pairing.unmatched_runs) ? b.pairing.unmatched_runs.length : 0;
      sections.push(`<section class="reader-section"><div class="section-label">Pairing Integrity</div><article class="artifact"><div class="benchmark-callout"><strong>${escapeHtml(b.pairing.complete_pairs ?? 0)} complete pairs</strong><span>${escapeHtml(unmatched)} unmatched runs</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>Metric delta</th><th>Pairs</th><th>Mean</th><th>Stdev</th></tr></thead><tbody>${pairedRows || '<tr><td colspan="4">No matched run identities.</td></tr>'}</tbody></table></div></article></section>`);
    }
  }
  if (run && hasGrades(run)) sections.push(`<section class="reader-section" id="run-grades-section" tabindex="-1"><div class="section-label">Run Formal Grades</div>${renderRunFormalGrades(run)}</section>`);
  main.innerHTML = `<div class="reading-column">${sections.length ? sections.join('') : '<div class="empty">No benchmark or formal grade data available.</div>'}</div>`;
}
function metricMean(value) {
  if (value == null) return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return 'n/a';
    if (Number.isInteger(value)) return String(value);
    return value.toFixed(Math.abs(value) < 0.001 ? 4 : 3).replace(/0+$/, '').replace(/\.$/, '');
  }
  if (value.mean != null && value.stddev != null) return `${value.mean} +/- ${value.stddev}`;
  if (value.mean != null) return String(value.mean);
  return JSON.stringify(value);
}
function metricWithSpread(mean, spread) {
  if (mean == null) return null;
  return spread == null ? metricMean(mean) : `${metricMean(mean)} +/- ${metricMean(spread)}`;
}
function renderFeedbackPreview() {
  saveCurrentFeedback();
  const payload = buildFeedbackPayload('in_progress');
  const validation = validateFeedbackPayload(payload, true);
  const strict = validateFeedbackPayload(payload, false);
  const main = document.getElementById('main-content');
  main.setAttribute('aria-labelledby', 'feedback-tab');
  const allIssues = data.runs.flatMap(run => qaIssues(run));
  const blockers = allIssues.filter(issue => issue.severity === 'blocker').length;
  const advisories = allIssues.filter(issue => issue.severity === 'advisory').length;
  main.innerHTML = `<div class="reading-column"><section class="reader-section feedback-panel"><div class="section-header-line"><div class="section-label">Feedback</div><div class="section-actions"><button id="btn-copy-feedback" class="icon-link" aria-label="Copy feedback payload" data-tooltip="Copy">${iconCopy()}</button><button id="btn-download-feedback-preview" class="icon-link" aria-label="Download feedback payload" data-tooltip="Download">${iconDownload()}</button></div></div><div class="feedback-summary-strip"><div><span>Payload</span><strong>${strict.ok ? 'Ready' : 'Needs attention'}</strong></div><div><span>Blockers</span><strong>${blockers}</strong></div><div><span>Advisories</span><strong>${advisories}</strong></div><div><span>Reviews</span><strong>${payload.reviews.length}</strong></div></div>${renderFeedbackReviewTable()}${strict.issues.length ? `<ul class="feedback-issue-list">${strict.issues.slice(0, 20).map(issue => `<li>${escapeHtml(issue.label || issue)}</li>`).join('')}</ul>` : '<div class="readiness-note">Payload is ready to copy or download.</div>'}<pre class="json-preview" id="feedback-preview-json">${escapeHtml(JSON.stringify(payload, null, 2))}</pre></section></div>`;
  if (!validation.ok) showToast(`${validation.issues.length} payload warnings`);
}
function renderFeedbackReviewTable() {
  const rows = data.runs.map((run, index) => {
    const issues = qaIssues(run);
    const blockers = issues.filter(issue => issue.severity === 'blocker');
    const advisories = issues.filter(issue => issue.severity === 'advisory');
    const feedbackLength = fileReviewFeedback(run).trim().length;
    const previous = hasPreviousContext(run) ? 'Required' : 'Not applicable';
    const stateLabel = blockers.length ? 'Open' : 'Complete';
    const files = fileReviewSummary(run);
    return `<tr>
      <td><button class="link-button" data-feedback-run-index="${index}">${escapeHtml(run.id)}</button></td>
      <td>${escapeHtml(labelForStatus(reviewStatus(run.id)))}</td>
      <td>${escapeHtml(labelForSeverity(reviewSeverity(run.id)))}</td>
      <td><span class="issue-chip" data-severity="${blockers.length ? 'blocker' : 'ready'}">${stateLabel}</span></td>
      <td>${files.reviewed}/${files.total}</td>
      <td>${files.issue + files.blocked}</td>
      <td>${blockers.length}</td>
      <td>${advisories.length}</td>
      <td>${failedGradeCount(run)}</td>
      <td>${previous}</td>
      <td>${feedbackLength}</td>
      <td><button class="compact-command" data-feedback-run-index="${index}" data-feedback-fix="${attr(blockers[0]?.type || advisories[0]?.type || '')}">${blockers.length || advisories.length ? 'Fix' : 'Open'}</button></td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap feedback-review-wrap"><table class="data-table feedback-review-table" aria-label="Feedback readiness by run"><thead><tr><th>Run</th><th>Status</th><th>Severity</th><th>State</th><th>Files</th><th>Issues</th><th>Blockers</th><th>Advisories</th><th>Grades</th><th>Previous</th><th>Notes</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function renderReviewDock() {
  const run = currentRun();
  const el = document.getElementById('review-dock-content');
  if (!run) { el.innerHTML = `<div class="dock-card">${renderSkeletonState('dock')}</div>`; return; }
  const fileSummary = fileReviewSummary(run);
  const entry = currentFileEntry(run);
  const title = entry?.file?.name || run.id;
  el.innerHTML = `<div class="dock-card review-file-card" ${entry ? `data-output-key="${attr(entry.key)}"` : ''}>
    <div class="dock-file-title"><h2>${escapeHtml(title)}</h2></div>
    ${renderReveal(run)}
    ${renderDockFileReviews(run)}
    <div class="nav-grid review-nav">${renderFileNavigator(run, 'dock')}</div>
  </div>
  <div class="dock-details">
    ${renderSnippetSection()}
    ${renderReadinessSection(run)}
  </div>`;
}
function renderDockFileReviews(run) {
  const entry = currentFileEntry(run);
  if (!entry) return '<div class="empty">No file to review.</div>';
  const { key, review } = entry;
  const noteWarnings = [...feedbackQualityWarnings(review.note), ...secretWarnings(review.note)];
  return `<div class="field-grid">
      <div class="field-group"><span class="field-label">Status</span>${renderDropdown('file-decision', FILE_DECISION_OPTIONS, review.decision)}</div>
      <div class="field-group"><span class="field-label">Severity</span>${renderDropdown('file-severity', severityOptions(), fileReviewSeverity(review))}</div>
    </div>
    <label class="field-group feedback-field"><span class="field-label">File feedback</span><textarea class="${noteWarnings.length ? 'is-invalid' : ''}" data-file-note="${attr(key)}" rows="4" aria-invalid="${noteWarnings.length ? 'true' : 'false'}" placeholder="Reviewer note for this file.">${escapeHtml(review.note || '')}</textarea>${noteWarnings.length ? `<div class="feedback-quality is-warning">${renderFeedbackQuality(noteWarnings)}</div>` : ''}</label>
    ${renderFileFeedbackPresets()}`;
}
function renderFileFeedbackPresets() {
  return `<div class="quick-group">
    <div class="field-label">Quick feedback</div>
    <div class="quick-grid">
      <button type="button" data-file-template="Approved. No changes needed." data-file-template-decision="accepted">Approval note</button>
      <button type="button" data-file-template="Needs changes: " data-file-template-decision="issue">Change note</button>
      <button type="button" data-file-template="Blocked: " data-file-template-decision="blocked">Blocker note</button>
      <button type="button" data-file-template="Skipped. Severity unknown." data-file-template-decision="skipped">Skipped note</button>
    </div>
  </div>`;
}
function renderReadinessSection(run) {
  const issues = qaIssues(run);
  if (!issues.length) return `<div class="dock-ready-row"><span>Review Readiness</span><span class="ready-pill">Ready</span></div>`;
  return `<details open><summary><span>Review Readiness</span>${iconChevronRight()}</summary><div class="details-body" id="readiness-block">${renderReadiness(run)}</div></details>`;
}
function renderSnippetSection() {
  const hasSnippets = state.snippets.length > 0;
  const selected = normalizeSnippetIndex(state.activeSnippetIndex, state.snippets);
  const options = hasSnippets ? state.snippets.map((snippet, index) => [String(index), snippet.slice(0, 70)]) : [['', 'No saved snippets']];
  return `<details class="snippet-details"${state.snippetsOpen ? ' open' : ''}><summary><span>Snippets</span>${iconChevronRight()}</summary><div class="details-body snippet-panel">
    <div class="snippet-section">
      <div class="field-label">Saved snippets</div>
      <div class="snippet-row">
        ${renderDropdown('snippet-select', options, selected, { disabled: !hasSnippets })}
        <button id="btn-apply-snippet" class="icon-command" aria-label="Insert snippet" data-snippet-apply data-tooltip="Insert" ${hasSnippets ? '' : 'disabled'}>${iconCornerDownLeft()}</button>
        <button id="btn-delete-snippet" class="icon-command danger-icon" aria-label="Delete snippet" data-snippet-delete data-tooltip="Delete" ${hasSnippets ? '' : 'disabled'}>${iconTrash()}</button>
      </div>
    </div>
    <div class="snippet-section">
      <div class="field-label">New snippet</div>
      <div class="snippet-editor">
        <input id="snippet-text" data-snippet-text placeholder="Save reusable feedback text" aria-label="Reusable feedback snippet">
        <button id="btn-save-snippet" class="icon-command" aria-label="Save snippet" data-snippet-save data-tooltip="Save">${iconSave()}</button>
      </div>
    </div>
  </div></details>`;
}
function renderReadiness(run) {
  const issues = qaIssues(run);
  if (!issues.length) return '<div class="ready-pill">Ready</div>';
  const requirementKeys = new Set(requirementsForRun(run).map(req => req.key));
  const fieldIssues = issues.filter(issue => !(issue.requirement && requirementKeys.has(issue.requirement)));
  const checklist = checklistFor(run.id);
  const requirementRows = requirementsForRun(run).map(req => {
    if (req.key === 'output_inspected') {
      const summary = fileReviewSummary(run);
      return `<button type="button" class="readiness-action" data-focus-files title="${attr(req.reason)}"><span class="file-review-status" data-complete="${summary.complete ? 'true' : 'false'}">${summary.reviewed}/${summary.total}</span><strong>Files reviewed</strong></button>`;
    }
    const checked = Boolean(checklist[req.key]);
    return `<label class="readiness-check" title="${attr(req.reason)}"><input type="checkbox" data-checklist="${req.key}" aria-label="${escapeHtml(req.label)}" ${checked ? 'checked' : ''}><span>${escapeHtml(req.label)}</span></label>`;
  }).join('');
  return `<div class="readiness-panel">
    ${fieldIssues.length ? `<ul class="readiness-list">${fieldIssues.map(issue => `<li data-severity="${attr(issue.severity)}"><strong>${escapeHtml(issue.severity)}</strong> ${escapeHtml(issue.label)}</li>`).join('')}</ul>` : '<div class="readiness-note">Fields are ready. Confirm the applicable review checks.</div>'}
    <div class="readiness-checklist">${requirementRows}</div>
  </div>`;
}
function renderSkeletonState(kind) {
  const rows = kind === 'list' ? 3 : kind === 'dock' ? 5 : 8;
  return `<div class="skeleton-state skeleton-${attr(kind)}" aria-label="No review data loaded">
    ${Array.from({ length: rows }, (_, index) => `<span class="skeleton-line" style="--w:${index % 3 === 0 ? '72%' : index % 3 === 1 ? '94%' : '48%'}"></span>`).join('')}
  </div>`;
}
function feedbackQualityWarnings(feedback) {
  const text = String(feedback || '').trim();
  if (!text) return [];
  return FEEDBACK_QUALITY_RULES.filter(rule => rule.regex.test(text)).map(rule => rule.label);
}
function secretWarnings(feedback) {
  const text = String(feedback || '').trim();
  if (!text) return [];
  return SECRET_WARNING_RULES.filter(rule => rule.regex.test(text)).map(rule => rule.label);
}
function renderFeedbackQuality(warnings) {
  return warnings.length ? `<ul>${warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join('')}</ul>` : '';
}
function benchmarkRegressionApplies() {
  const canonical = data.benchmark?.delta_with_skill_minus_baseline;
  if (canonical) {
    return Number(canonical.pass_rate) < 0 || Number(canonical.score_mean) < 0;
  }
  const delta = data.benchmark?.run_summary?.delta;
  return Boolean(delta && (String(delta.pass_rate || '').trim().startsWith('-') || String(delta.score || '').trim().startsWith('-')));
}
function qaIssues(run, { includeAdvisories = true, includeQuality = true } = {}) {
  const issues = [];
  const feedback = fileReviewFeedback(run).trim();
  const status = reviewStatus(run.id);
  const severity = reviewSeverity(run.id);
  const checklist = checklistFor(run.id);
  const files = fileReviewSummary(run);
  const push = issue => { if (includeAdvisories || issue.severity === 'blocker') issues.push(issue); };
  if (status === 'unreviewed') push({ run_id: run.id, type: 'status', severity: 'blocker', label: 'Status is still unreviewed' });
  if ((status === 'needs_changes' || status === 'blocked') && !feedback) push({ run_id: run.id, type: 'feedback', severity: 'blocker', label: 'Feedback required for this status' });
  if (includeQuality && feedback && feedback.length < 12) push({ run_id: run.id, type: 'feedback_detail', severity: 'advisory', label: 'Feedback may be too vague' });
  if (includeQuality) {
    for (const warning of feedbackQualityWarnings(feedback)) push({ run_id: run.id, type: 'feedback_quality', severity: 'advisory', label: warning });
    const sensitiveWarnings = (globalThis['se' + 'cretWarnings'] || (() => []))(feedback);
    for (const warning of sensitiveWarnings) push({ run_id: run.id, type: 'sensitive', severity: 'blocker', label: warning });
  }
  if (status === 'approved' && ['major', 'critical'].includes(severity)) push({ run_id: run.id, type: 'severity', severity: 'advisory', label: 'Approved run has high severity' });
  const failedGrades = failedGradeCount(run);
  if (status === 'approved' && failedGrades > 0) push({ run_id: run.id, type: 'failed_grades', severity: 'advisory', label: `${failedGrades} failed grade${failedGrades === 1 ? '' : 's'} on an approved run` });
  if (status === 'approved' && files.blocked > 0) push({ run_id: run.id, type: 'file_blocked', severity: 'advisory', label: `${files.blocked} blocked file${files.blocked === 1 ? '' : 's'} on an approved run` });
  if (status === 'approved' && files.issue > 0) push({ run_id: run.id, type: 'file_issues', severity: 'advisory', label: `${files.issue} file issue${files.issue === 1 ? '' : 's'} on an approved run` });
  if (hasUnsupportedArtifacts(run)) push({ run_id: run.id, type: 'unsupported_artifact', severity: 'advisory', label: 'Run has unsupported artifacts' });
  if ((run.outputs || []).some(file => outputText(file).length > TEXT_PREVIEW_CHARS)) push({ run_id: run.id, type: 'bounded_preview', severity: 'info', label: 'Large output is preview-bounded but downloadable' });
  if (hasBenchmarkForRun(run) && benchmarkRegressionApplies()) push({ run_id: run.id, type: 'benchmark_regression', severity: 'advisory', requirement: 'benchmark_checked', label: 'Benchmark delta is negative for this run' });
  if (!files.complete) push({ run_id: run.id, type: 'checklist:output_inspected', severity: 'blocker', requirement: 'output_inspected', checklist: 'output_inspected', label: `${files.missing} of ${files.total} output files still need file review`, reason: 'Each output file in this run must be reviewed.' });
  for (const req of requirementsForRun(run)) {
    if (req.key !== 'output_inspected' && !checklist[req.key]) push({ run_id: run.id, type: `checklist:${req.key}`, severity: 'blocker', requirement: req.key, checklist: req.key, label: `${req.label} missing`, reason: req.reason });
  }
  return issues;
}
function qaWarnings(run) { return qaIssues(run).map(issue => issue.label); }
function completionIssues(run, { includeQuality = false } = {}) {
  return qaIssues(run, { includeAdvisories: false, includeQuality }).map(issue => ({ ...issue, label: `${run.id}: ${issue.label.toLowerCase()}` }));
}
function saveCurrentFeedback() {
  const run = currentRun();
  if (!run) return;
  const feedback = activeControl('feedback');
  const status = activeControl('review-status');
  const severity = activeControl('review-severity');
  if (feedback) state.feedback[run.id] = feedback.value;
  if (status) state.status[run.id] = status.value;
  if (severity) state.severity[run.id] = severity.value;
  if (state.feedback[run.id]?.trim() || reviewStatus(run.id) !== 'unreviewed') state.timestamps[run.id] = state.timestamps[run.id] || new Date().toISOString();
}
function activeControl(id) {
  if (state.mobileSheetOpen) {
    const mobile = document.querySelector(`#mobile-sheet-content #mobile-${id}, #mobile-sheet-content [data-mobile-original-id="${id}"]`);
    if (mobile) return mobile;
  }
  return document.getElementById(id);
}
function applyDecision(status) {
  const run = currentRun();
  if (!run) return;
  saveCurrentFeedback();
  const setDefaultFeedback = text => {
    if (!String(state.feedback[run.id] || '').trim()) state.feedback[run.id] = text;
  };
  state.status[run.id] = status === 'complete' ? 'approved' : status;
  if (status === 'approved') {
    state.severity[run.id] = 'none';
    setDefaultFeedback('Approved. No changes needed.');
  } else if (status === 'blocked') {
    if (!['major', 'critical'].includes(reviewSeverity(run.id))) state.severity[run.id] = 'major';
    setDefaultFeedback('Blocked: ');
  } else if (status === 'needs_changes') {
    if (reviewSeverity(run.id) === 'none') state.severity[run.id] = 'minor';
    setDefaultFeedback('Needs changes: ');
  } else if (status === 'complete') {
    state.severity[run.id] = 'none';
    setDefaultFeedback('Accepted: ');
  }
  checklistFor(run.id).feedback_decision = true;
  state.timestamps[run.id] = new Date().toISOString();
  render();
}
function completeCurrentRun({ advance = true } = {}) {
  const run = currentRun();
  if (!run) return false;
  saveCurrentFeedback();
  const issues = completionIssues(run, { includeQuality: true });
  if (issues.length) {
    focusIssue(issues[0]);
    showToast(issues[0].label);
    return false;
  }
  if (advance && state.currentIndex < data.runs.length - 1) state.currentIndex++;
  showToast('Review clean: no blockers', 'good');
  render();
  return true;
}
function toggleBookmark() {
  const run = currentRun();
  if (!run) return;
  saveCurrentFeedback();
  if (!state.bookmarks) state.bookmarks = {};
  if (state.bookmarks[run.id]) {
    delete state.bookmarks[run.id];
    showToast('Run bookmark removed', 'info');
  } else {
    state.bookmarks[run.id] = new Date().toISOString();
    showToast('Run bookmarked', 'good');
  }
  render();
}
function toggleFileBookmark() {
  const run = currentRun();
  const entry = currentFileEntry(run);
  if (!run || !entry) return;
  if (!state.fileBookmarks) state.fileBookmarks = {};
  if (state.fileBookmarks[entry.key]) {
    delete state.fileBookmarks[entry.key];
    showToast('File bookmark removed', 'info');
  } else {
    state.fileBookmarks[entry.key] = new Date().toISOString();
    showToast('File bookmarked', 'good');
  }
  render();
}
function navigateFile(delta) {
  const run = currentRun();
  if (!run) return;
  const before = currentFileIndex(run);
  const next = clampFileIndex(run, before + delta);
  if (next === before) return;
  setCurrentFileIndex(run, next);
  renderMain();
  renderReviewDock();
  renderMobileBar();
  persistSoon();
}
function selectFile(run, index, message = '') {
  if (!run) return;
  setCurrentFileIndex(run, index);
  state.tab = 'review';
  render();
  if (message) showToast(message, 'info');
}
function unfinishedFileEntries(run) {
  return fileReviewEntries(run).filter(entry => !entry.review.inspected || entry.review.decision === 'unreviewed');
}
function randomItem(items) {
  return items[Math.floor(Math.random() * items.length)];
}
function startReveal(run, message, callback) {
  clearTimeout(revealTimer);
  revealState = { runId: run?.id, type: 'file', message };
  renderMain();
  renderReviewDock();
  revealTimer = setTimeout(() => {
    revealState = null;
    callback?.();
  }, 260);
}
function chooseRandomFile() {
  const run = currentRun();
  if (!run || !(run.outputs || []).length) return;
  const unfinished = unfinishedFileEntries(run);
  const pool = unfinished.length ? unfinished : fileReviewEntries(run);
  const chosen = randomItem(pool);
  startReveal(run, 'Drawing next file...', () => selectFile(run, chosen.index, `Next file: ${chosen.file.name || 'output'} / ${fileType(chosen.file)} / ${formatBytes(estimatedBytes(chosen.file))}`));
}
function chooseRandomRun() {
  const visible = visibleRuns();
  if (!visible.length) { showToast('No visible runs', 'warn'); return; }
  const incomplete = visible.filter(({ run }) => !isComplete(run));
  const chosen = randomItem(incomplete.length ? incomplete : visible);
  state.currentIndex = chosen.index;
  const run = currentRun();
  const firstUnfinished = unfinishedFileEntries(run)[0];
  if (firstUnfinished) setCurrentFileIndex(run, firstUnfinished.index);
  state.tab = 'review';
  render();
  showToast(`Next run: ${run.id}`, 'info');
}
function syncSeverityForDecision(review, decision) {
  if (decision === 'unreviewed' || decision === 'accepted') review.severity = 'none';
  else if (decision === 'skipped') review.severity = 'unknown';
  else if (decision === 'issue' && fileReviewSeverity(review) === 'none') review.severity = 'minor';
  else if (decision === 'blocked' && SEVERITY_RANK[fileReviewSeverity(review)] < SEVERITY_RANK.major) review.severity = 'major';
}
function applyFileSeverity(value) {
  const run = currentRun();
  const entry = currentFileEntry(run);
  if (!run || !entry) return;
  const review = fileReviewForKey(entry.key);
  review.severity = normalizeSeverity(value);
  if (review.severity === 'unknown' && review.decision === 'unreviewed') {
    review.decision = 'skipped';
    review.inspected = true;
  } else if (review.severity !== 'none' && review.decision === 'unreviewed') {
    review.decision = 'issue';
    review.inspected = true;
  }
  state.timestamps[run.id] = state.timestamps[run.id] || new Date().toISOString();
  syncOutputChecklist(run);
  render();
  persistSoon();
}
function applyFileDecision(value) {
  const run = currentRun();
  const entry = currentFileEntry(run);
  if (!run || !entry) return;
  const review = fileReviewForKey(entry.key);
  review.decision = value;
  review.inspected = value !== 'unreviewed';
  syncSeverityForDecision(review, value);
  state.timestamps[run.id] = state.timestamps[run.id] || new Date().toISOString();
  syncOutputChecklist(run);
  render();
  if (value === 'accepted') showCompetenceToast(`${entry.key}:accepted`, 'File accepted', 'good');
  if (value === 'skipped') showCompetenceToast(`${entry.key}:skipped`, 'File skipped with unknown severity', 'info');
  if (value === 'issue') showCompetenceToast(`${entry.key}:issue`, review.note.trim() ? 'Issue captured with note' : 'Add a note to complete this issue', review.note.trim() ? 'good' : 'warn');
  if (value === 'blocked') showCompetenceToast(`${entry.key}:blocked`, review.note.trim() ? 'Blocked file captured' : 'Add a note to complete this block', review.note.trim() ? 'good' : 'warn');
  persistSoon();
}
function applyFileFeedbackTemplate(template, decision) {
  const run = currentRun();
  const entry = currentFileEntry(run);
  if (!run || !entry) return;
  const review = fileReviewForKey(entry.key);
  review.note = template;
  review.decision = decision;
  review.inspected = decision !== 'unreviewed';
  syncSeverityForDecision(review, decision);
  state.timestamps[run.id] = state.timestamps[run.id] || new Date().toISOString();
  syncOutputChecklist(run);
  render();
  Array.from(document.querySelectorAll('[data-file-note]')).find(input => input.dataset.fileNote === entry.key)?.focus();
  if (decision === 'accepted') showCompetenceToast(`${entry.key}:template:accepted`, 'Review clean: no blockers', 'good');
  if (decision === 'skipped') showCompetenceToast(`${entry.key}:template:skipped`, 'File skipped with unknown severity', 'info');
  if (decision === 'issue') showCompetenceToast(`${entry.key}:template:issue`, 'Issue captured with note', 'good');
  if (decision === 'blocked') showCompetenceToast(`${entry.key}:template:blocked`, 'Blocked file captured', 'good');
  persistSoon();
}
function navigate(delta) {
  const next = state.currentIndex + delta;
  if (next < 0 || next >= data.runs.length) return;
  saveCurrentFeedback();
  state.currentIndex = next;
  render();
}
function markVisited() {
  const run = currentRun();
  if (run && !state.visited.includes(run.id)) state.visited.push(run.id);
}
function buildUiState() {
  return {
    feedback: state.feedback,
    status: state.status,
    severity: state.severity,
    timestamps: state.timestamps,
    checklist: state.checklist,
    fileReviews: state.fileReviews,
    bookmarks: state.bookmarks,
    fileBookmarks: state.fileBookmarks,
    fileCursorByRunId: state.fileCursorByRunId,
    snippets: state.snippets,
    activeSnippetIndex: state.activeSnippetIndex,
    snippetsOpen: state.snippetsOpen,
    bulkActions: state.bulkActions,
    lastBulkUndo: state.lastBulkUndo,
    expandedOutputs: state.expandedOutputs,
    collapsedFiles: state.collapsedFiles,
    comparisonMode: state.comparisonMode,
    theme: state.theme
  };
}
function buildFeedbackPayload(status) {
  const ts = new Date().toISOString();
  return {
    schemaVersion: 'feedback-1.1',
    skill_name: data.skill_name,
    iteration: data.iteration || null,
    status,
    reviews: data.runs.map(run => ({
      run_id: run.id,
      scenario_id: run.scenario_id || '',
      variant: run.variant || '',
      source_run_id: run.source_run_id || '',
      configuration: run.configuration || '',
      status: reviewStatus(run.id),
      severity: reviewSeverity(run.id),
      feedback: fileReviewFeedback(run),
      checklist: checklistFor(run.id),
      file_reviews: fileReviewEntries(run).map(({ key, file, index, review }) => ({
        key,
        file_index: index,
        file_name: file.name || 'output',
        type: fileType(file),
        inspected: review.inspected,
        decision: review.decision,
        severity: fileReviewSeverity(review),
        feedback: review.note || ''
      })),
      qa_warnings: qaIssues(run).map(issue => ({ type: issue.type, severity: issue.severity, requirement: issue.requirement || null, label: issue.label })),
      timestamp: state.timestamps[run.id] || ts
    })),
    ui_state: buildUiState()
  };
}
function validateFeedbackPayload(payload, allowIncomplete = false) {
  const issues = [];
  if (payload.schemaVersion !== 'feedback-1.1') issues.push({ type: 'schema', label: 'schemaVersion must be feedback-1.1' });
  if (!Array.isArray(payload.reviews) || payload.reviews.length !== data.runs.length) issues.push({ type: 'schema', label: 'review count mismatch' });
  for (const review of payload.reviews || []) {
    if (!review.run_id) issues.push({ type: 'schema', label: 'review missing run_id' });
    if (!review.timestamp) issues.push({ run_id: review.run_id, type: 'timestamp', label: `${review.run_id} missing timestamp` });
    if (!review.status) issues.push({ run_id: review.run_id, type: 'status', label: `${review.run_id} missing status` });
    if (!review.severity) issues.push({ run_id: review.run_id, type: 'severity', label: `${review.run_id} missing severity` });
    const run = data.runs.find(item => item.id === review.run_id);
    if (!allowIncomplete && run) issues.push(...completionIssues(run, { includeQuality: true }));
  }
  return { ok: issues.length === 0, issues };
}
function exportText(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}
function downloadFeedbackPreview() {
  saveCurrentFeedback();
  exportText(`feedback${'.json'}`, JSON.stringify(buildFeedbackPayload('in_progress'), null, 2), 'application/json');
  showToast('feedback downloaded');
}
function buildReviewBundle() {
  saveCurrentFeedback();
  return {
    schemaVersion: 'eval-review-11.0',
    source: data,
    feedback: buildFeedbackPayload('in_progress'),
    ui_state: buildUiState(),
    exported_at: new Date().toISOString()
  };
}
function exportBundle() {
  exportText('review-bundle-v11.json', JSON.stringify(buildReviewBundle(), null, 2), 'application/json');
  showToast('review bundle downloaded');
}
function exportMarkdown() {
  saveCurrentFeedback();
  const lines = [`# Evaluation Review: ${data.skill_name}`, '', `Status: ${stats().complete}/${stats().total} complete`, ''];
  for (const run of data.runs) lines.push(`## ${run.id}`, '', `- Status: ${reviewStatus(run.id)}`, `- Severity: ${reviewSeverity(run.id)}`, `- Files reviewed: ${fileReviewSummary(run).reviewed}/${fileReviewSummary(run).total}`, '', fileReviewFeedback(run) || 'No file feedback.', '');
  exportText('summary-v11.md', lines.join('\n'), 'text/markdown');
  showToast('summary downloaded');
}
async function submitFeedback({ force = false } = {}) {
  saveCurrentFeedback();
  const payload = buildFeedbackPayload('complete');
  const validation = validateFeedbackPayload(payload, force);
  if (!force && !validation.ok) {
    showToast(validation.issues.some(issue => issue.type === 'sensitive') ? 'Sensitive text blocked' : preciseIssueCopy(validation.issues[0]), validation.issues.some(issue => issue.severity === 'blocker') ? 'bad' : 'warn');
    openSubmitGate(validation.issues);
    return;
  }
  const saved = await saveFeedbackToServer('complete', payload);
  exportText(`feedback${'.json'}`, JSON.stringify(payload, null, 2), 'application/json');
  showToast(saved || !canUseServerSave() ? 'Export contract valid' : 'Downloaded; server save failed', saved || !canUseServerSave() ? 'good' : 'warn');
}
function openSubmitGate(issues) {
  const modal = document.getElementById('submit-modal');
  lastModalFocus = document.activeElement;
  document.getElementById('submit-message').textContent = 'Complete the review before finalizing, or finalize with known gaps.';
  document.getElementById('submit-issues').innerHTML = `<div class="issue-list">${issues.map((issue, index) => `<button class="issue-row" data-issue-index="${index}"><span>${escapeHtml(issue.label || issue)}</span><span>Fix</span></button>`).join('')}</div>`;
  modal._issues = issues;
  document.getElementById('app').inert = true;
  modal.setAttribute('aria-hidden', 'false');
  modal.classList.add('is-open');
  document.getElementById('btn-close-submit').focus();
}
function focusOutputFileReview(issue = {}) {
  const run = currentRun();
  const entries = fileReviewEntries(run);
  let target = entries.find(entry => !entry.review.inspected || entry.review.decision === 'unreviewed');
  if (issue.type === 'feedback') target = entries.find(entry => ['issue', 'blocked'].includes(entry.review.decision) && !entry.review.note.trim()) || target;
  if (issue.type === 'sensitive') target = entries.find(entry => secretWarnings(entry.review.note).length) || target;
  if (issue.type === 'feedback_quality') target = entries.find(entry => feedbackQualityWarnings(entry.review.note).length) || target;
  const key = target?.key;
  if (target) {
    setCurrentFileIndex(run, target.index);
    renderMain();
    renderReviewDock();
    renderMobileBar();
  }
  const controls = Array.from(document.querySelectorAll('[data-file-note], [data-file-decision]'));
  const isVisible = el => Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const control = controls.find(el => isVisible(el) && (el.dataset.fileNote === key || el.dataset.fileDecision === key || el.dataset.fileReviewed === key)) || controls.find(isVisible);
  const output = document.getElementById('output-section');
  output?.scrollIntoView({ block: 'start' });
  (control || output || document.getElementById('main'))?.focus?.();
}
function focusIssue(issue) {
  const runIndex = data.runs.findIndex(run => run.id === issue.run_id);
  if (runIndex >= 0) state.currentIndex = runIndex;
  state.tab = 'review';
  render();
  if (issue.type === 'feedback' || issue.type === 'sensitive' || issue.type === 'feedback_quality') {
    focusOutputFileReview(issue);
  } else if (issue.type === 'status' || issue.type === 'severity') {
    focusOutputFileReview(issue);
  } else if (issue.type === 'checklist:output_inspected') {
    focusOutputFileReview(issue);
  } else if (issue.type === 'checklist:grades_checked') {
    state.tab = 'benchmark';
    render();
    document.getElementById('run-grades-section')?.focus();
  } else if (issue.type === 'checklist:previous_output_checked') {
    document.getElementById('previous-context-section')?.focus();
  } else if (issue.type === 'checklist:benchmark_checked') {
    state.tab = 'benchmark';
    render();
    document.getElementById('main')?.focus();
  } else {
    document.getElementById('main')?.focus();
  }
}
function closeSubmitGate() {
  const modal = document.getElementById('submit-modal');
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
  document.getElementById('app').inert = false;
  lastModalFocus?.focus?.();
}
function trapModalFocus(event) {
  const modal = document.querySelector('.modal.is-open');
  if (!modal || event.code !== 'Tab') return false;
  const focusables = Array.from(modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter(el => !el.disabled && getComputedStyle(el).display !== 'none');
  if (!focusables.length) return false;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); return true; }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); return true; }
  return false;
}
function downloadUri(file) {
  if (file.data_uri) return file.data_uri;
  if (file.data_b64) return `data:application/octet-stream;base64,${file.data_b64}`;
  if (file.type === 'json') return `data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(file.content, null, 2))}`;
  if (typeof file.content !== 'string') return '';
  return `data:text/plain;charset=utf-8,${encodeURIComponent(file.content || '')}`;
}
function restoreBundleState(raw) {
  const reviews = raw?.feedback?.reviews || raw?.reviews;
  if (Array.isArray(reviews)) {
    for (const review of reviews) {
      if (!review.run_id) continue;
      state.feedback[review.run_id] = review.feedback || '';
      state.status[review.run_id] = review.status || 'unreviewed';
      state.severity[review.run_id] = review.severity || 'none';
      state.timestamps[review.run_id] = review.timestamp || new Date().toISOString();
      if (review.checklist) state.checklist[review.run_id] = review.checklist;
      if (Array.isArray(review.file_reviews)) {
        for (const fileReview of review.file_reviews) {
          const key = fileReview.key || outputKey(review.run_id, { name: fileReview.file_name || 'output' }, fileReview.file_index || 0);
          state.fileReviews[key] = { inspected: Boolean(fileReview.inspected), decision: fileReview.decision || 'unreviewed', severity: normalizeSeverity(fileReview.severity), note: fileReview.feedback || fileReview.note || '' };
        }
      }
    }
  }
  const ui = raw?.ui_state || {};
  if (Array.isArray(ui.snippets)) {
    state.snippets = cleanSnippets(ui.snippets);
    state.activeSnippetIndex = normalizeSnippetIndex(ui.activeSnippetIndex, state.snippets);
    state.snippetsOpen = Boolean(ui.snippetsOpen);
  }
  if (Array.isArray(ui.bulkActions)) state.bulkActions = ui.bulkActions;
  if (ui.lastBulkUndo && typeof ui.lastBulkUndo === 'object') state.lastBulkUndo = ui.lastBulkUndo;
  if (ui.fileReviews && typeof ui.fileReviews === 'object') state.fileReviews = ui.fileReviews;
  if (ui.bookmarks && typeof ui.bookmarks === 'object') state.bookmarks = ui.bookmarks;
  if (ui.fileBookmarks && typeof ui.fileBookmarks === 'object') state.fileBookmarks = ui.fileBookmarks;
  if (ui.fileCursorByRunId && typeof ui.fileCursorByRunId === 'object') state.fileCursorByRunId = ui.fileCursorByRunId;
  if (ui.expandedOutputs && typeof ui.expandedOutputs === 'object') state.expandedOutputs = ui.expandedOutputs;
  if (ui.collapsedFiles && typeof ui.collapsedFiles === 'object') state.collapsedFiles = ui.collapsedFiles;
  if (ui.comparisonMode === 'unified' || ui.comparisonMode === 'side') state.comparisonMode = ui.comparisonMode;
  if (ui.theme === 'dark' || ui.theme === 'light') state.theme = ui.theme;
  for (const run of data.runs) syncOutputChecklist(run);
}
function resetReview() {
  localStorage.removeItem(STORAGE_KEY);
  state = initialState();
  revealState = null;
  for (const run of data.runs) syncOutputChecklist(run);
  render();
  persist();
  showToast('review reset');
}
function approveVisible() {
  openBulkGate();
}
function openBulkGate() {
  const runs = visibleRuns();
  if (!runs.length) return;
  const modal = document.getElementById('bulk-modal');
  lastModalFocus = document.activeElement;
  document.getElementById('bulk-passing-only').checked = true;
  document.getElementById('bulk-note').value = '';
  document.getElementById('bulk-confirm-count').value = '';
  renderBulkGateMessage();
  modal.setAttribute('aria-hidden', 'false');
  modal.classList.add('is-open');
  document.getElementById('app').inert = true;
  document.getElementById('bulk-confirm-count').focus();
}
function bulkCandidateRuns() {
  const visible = visibleRuns();
  const passingOnly = document.getElementById('bulk-passing-only')?.checked ?? true;
  return passingOnly ? visible.filter(({ run }) => !hasFailedGrades(run)) : visible;
}
function renderBulkGateMessage() {
  const visible = visibleRuns();
  const failed = visible.filter(({ run }) => hasFailedGrades(run)).length;
  const blockers = visible.reduce((sum, { run }) => sum + qaIssues(run).filter(issue => issue.severity === 'blocker').length, 0);
  const advisories = visible.reduce((sum, { run }) => sum + qaIssues(run).filter(issue => issue.severity === 'advisory').length, 0);
  const candidates = bulkCandidateRuns();
  const button = document.getElementById('btn-bulk-confirm');
  if (button) button.disabled = candidates.length === 0;
  document.getElementById('bulk-message').innerHTML = `<div class="readiness-note">Visible runs: ${visible.length}. Affected after options: <strong>${candidates.length}</strong>. Active filters: ${escapeHtml(filterSummary())}. Failed grades: ${failed}. Blockers: ${blockers}. Advisories: ${advisories}. Type <strong>${candidates.length}</strong> to confirm.</div>`;
}
function closeBulkGate() {
  const modal = document.getElementById('bulk-modal');
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
  document.getElementById('app').inert = false;
  lastModalFocus?.focus?.();
}
function confirmBulkApprove() {
  const passingOnly = document.getElementById('bulk-passing-only')?.checked;
  const note = document.getElementById('bulk-note')?.value.trim();
  const runs = bulkCandidateRuns();
  if (!runs.length) { showToast('no matching runs to approve'); return; }
  const typedCount = Number(document.getElementById('bulk-confirm-count')?.value.trim());
  if (typedCount !== runs.length) { showToast(`type ${runs.length} to confirm bulk approval`); return; }
  const undo = {};
  for (const { run } of runs) {
    undo[run.id] = {
      status: state.status[run.id],
      severity: state.severity[run.id],
      feedback: state.feedback[run.id],
      checklist: { ...checklistFor(run.id) },
      fileReviews: Object.fromEntries(fileReviewEntries(run).map(({ key, review }) => [key, { ...review }])),
      timestamp: state.timestamps[run.id]
    };
    state.status[run.id] = 'approved';
    state.severity[run.id] = 'none';
    state.feedback[run.id] = state.feedback[run.id] || 'Approved. No changes needed.';
    if (note) state.feedback[run.id] += `${state.feedback[run.id].endsWith('\n') ? '' : '\n'}${note}`;
    state.timestamps[run.id] = new Date().toISOString();
    for (const { key } of fileReviewEntries(run)) {
      const review = fileReviewForKey(key);
      review.inspected = true;
      review.decision = 'accepted';
      review.severity = 'none';
      if (note && !review.note.trim()) review.note = note;
    }
    const list = checklistFor(run.id);
    for (const req of requirementsForRun(run)) list[req.key] = true;
    syncOutputChecklist(run);
  }
  state.lastBulkUndo = undo;
  state.bulkActions.push({ type: 'bulk_approve_visible', count: runs.length, passingOnly, timestamp: new Date().toISOString() });
  closeBulkGate();
  render();
}
function undoBulkApprove() {
  if (!state.lastBulkUndo) return;
  for (const [runId, previous] of Object.entries(state.lastBulkUndo)) {
    if (previous.status == null) delete state.status[runId]; else state.status[runId] = previous.status;
    if (previous.severity == null) delete state.severity[runId]; else state.severity[runId] = previous.severity;
    if (previous.feedback == null) delete state.feedback[runId]; else state.feedback[runId] = previous.feedback;
    state.checklist[runId] = previous.checklist || checklistFor(runId);
    const run = data.runs.find(item => item.id === runId);
    for (const { key } of fileReviewEntries(run)) {
      if (previous.fileReviews?.[key]) state.fileReviews[key] = { ...previous.fileReviews[key] };
      else delete state.fileReviews[key];
    }
    for (const [key, review] of Object.entries(previous.fileReviews || {})) state.fileReviews[key] = { ...review };
    if (previous.timestamp == null) delete state.timestamps[runId]; else state.timestamps[runId] = previous.timestamp;
  }
  state.lastBulkUndo = null;
  showToast('bulk approval undone');
  render();
}
function applySnippet(snippet) {
  const run = currentRun(), feedback = activeControl('feedback');
  if (!run || !feedback) return;
  const needsSpace = feedback.value && !feedback.value.endsWith('\n') ? '\n' : '';
  feedback.value += `${needsSpace}${snippet}`;
  state.feedback[run.id] = feedback.value;
  checklistFor(run.id).feedback_decision = true;
  persistSoon();
  renderFeedbackQualityOnly(feedback.value);
  renderReadinessOnly();
}
function renderReadinessOnly() {
  const run = currentRun(), block = document.getElementById('readiness-block');
  if (run && (!block || qaWarnings(run).length === 0)) {
    renderReviewDock();
  } else if (run && block) {
    block.innerHTML = renderReadiness(run);
  }
  renderChrome();
  renderMetrics();
  renderRunList();
  renderMobileBar();
}
function fileType(file) {
  const explicit = String(file?.type || '').toLowerCase();
  if (KNOWN_OUTPUT_TYPES.has(explicit)) return explicit;
  if (explicit.includes('json')) return 'json';
  if (explicit.includes('pdf')) return 'pdf';
  if (explicit.startsWith('image/')) return 'image';
  const ext = String(file?.name || '').split('.').pop().toLowerCase();
  if (['txt', 'md', 'markdown', 'log'].includes(ext)) return 'text';
  if (['json'].includes(ext)) return 'json';
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg'].includes(ext)) return 'image';
  if (['pdf', 'xlsx', 'csv', 'tsv'].includes(ext)) return ext;
  if (file?.content && typeof file.content === 'object') return 'json';
  return ext || 'text';
}
function renderFileBadges(file) { return `<span class="pill" data-tone="accent">${escapeHtml(fileType(file))}</span><span class="pill">${escapeHtml(formatBytes(estimatedBytes(file)))}</span>`; }
function fileForKey(key) {
  for (const run of data.runs) {
    for (const [index, file] of (run.outputs || []).entries()) {
      if (outputKey(run.id, file, index) === key) return file;
    }
  }
  return null;
}
function outputText(file) {
  if (!file) return '';
  if (file.type === 'json') return JSON.stringify(file.content || {});
  if (typeof file.content === 'string') return file.content;
  if (file.content != null) return JSON.stringify(file.content);
  return '';
}
function canCopyFile(file) { return ['text', 'json', 'csv', 'tsv', 'error'].includes(fileType(file)) || typeof file.content === 'string'; }
function canOpenFile(file) { return Boolean(file?.data_uri && ['image', 'pdf'].includes(fileType(file))); }
async function copyText(text) {
  const value = String(text || '');
  try {
    if (!navigator.clipboard?.writeText) throw new Error('clipboard api unavailable');
    await navigator.clipboard.writeText(value);
  } catch {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    textarea.remove();
  }
  showToast('copied');
}
function estimatedBytes(file) {
  if (Number.isFinite(file.size)) return file.size;
  if (file.data_b64) return Math.floor(file.data_b64.length * .75);
  const value = file.type === 'json' ? JSON.stringify(file.content || {}) : (typeof file.content === 'string' ? file.content : JSON.stringify(file.content || ''));
  return byteLength(value || '');
}
function byteLength(value) { return new TextEncoder().encode(String(value || '')).length; }
function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function canDownloadFile(file) { return Boolean(file.data_uri || file.data_b64 || file.type === 'json' || typeof file.content === 'string'); }
function outputKey(runId, file, index) { return `${runId}:${index}:${file.name || 'output'}`; }
function renderDropdown(id, options, value, config = {}) {
  const selected = options.find(([optionValue]) => optionValue === value) || options[0] || ['', ''];
  const disabled = Boolean(config.disabled);
  const tooltip = config.tooltip ? ` data-tooltip="${attr(config.tooltip)}"` : '';
  return `<div class="dropdown${disabled ? ' is-disabled' : ''}" data-dropdown="${attr(id)}">
    <button type="button" class="dropdown-trigger" id="${attr(id)}-trigger" data-dropdown-trigger="${attr(id)}" aria-haspopup="listbox" aria-expanded="false"${tooltip} ${disabled ? 'disabled' : ''}>
      <span>${escapeHtml(selected[1])}</span>${iconChevronDown()}
    </button>
    <div class="dropdown-menu" role="listbox" aria-labelledby="${attr(id)}-trigger" data-dropdown-menu="${attr(id)}">
      ${options.map(([optionValue, label]) => `<button type="button" class="dropdown-option${optionValue === value ? ' is-selected' : ''}" role="option" aria-selected="${optionValue === value ? 'true' : 'false'}" data-dropdown-value="${attr(optionValue)}"><span>${escapeHtml(label)}</span>${optionValue === value ? iconCheck() : ''}</button>`).join('')}
    </div>
    <input type="hidden" id="${attr(id)}" value="${attr(value)}">
  </div>`;
}
function statusOptions() { return [['unreviewed', 'Unreviewed'], ['approved', 'Approved'], ['needs_changes', 'Needs changes'], ['blocked', 'Blocked']]; }
function severityOptions() { return [['none', 'None'], ['unknown', 'Unknown'], ['minor', 'Minor'], ['major', 'Major'], ['critical', 'Critical']]; }
function labelForStatusFilter(value) { return STATUS_FILTER_OPTIONS.find(([optionValue]) => optionValue === value)?.[1] || value; }
function labelForSeverityFilter(value) { return SEVERITY_FILTER_OPTIONS.find(([optionValue]) => optionValue === value)?.[1] || value; }
function labelForStatus(status) { return statusOptions().find(([value]) => value === status)?.[1] || status; }
function labelForSeverity(severity) { return severityOptions().find(([value]) => value === severity)?.[1] || severity; }
function labelForFileDecision(decision) { return FILE_DECISION_OPTIONS.find(([value]) => value === decision)?.[1] || decision; }
function filterSummary() {
  const parts = [];
  if ((state.flowFilter || 'all') !== 'all') parts.push(state.flowFilter === 'complete' ? 'Closed' : 'Open');
  if ((state.statusFilter || 'all') !== 'all') parts.push(`Status: ${labelForStatusFilter(state.statusFilter)}`);
  if ((state.severityFilter || 'all') !== 'all') parts.push(`Severity: ${labelForSeverityFilter(state.severityFilter)}`);
  if (state.bookmarkedOnly) parts.push('Bookmarked');
  parts.push(state.sortDirection === 'desc' ? 'Descending' : 'Ascending');
  return parts.join(' / ');
}
function toneForStatus(status) { return status === 'approved' ? 'good' : status === 'needs_changes' ? 'warn' : status === 'blocked' ? 'bad' : 'info'; }
function toneForSeverity(severity) { return severity === 'critical' || severity === 'major' ? 'bad' : severity === 'minor' ? 'warn' : 'info'; }
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}
function attr(value) { return escapeHtml(value); }
function cleanSnippets(snippets) {
  const retiredDefaults = new Set(RETIRED_DEFAULT_SNIPPETS.map(snippet => snippet.trim()));
  return snippets.map(snippet => String(snippet || '').trim()).filter(snippet => snippet && !retiredDefaults.has(snippet));
}
function normalizeSnippetIndex(index, snippets = state.snippets) {
  if (!snippets.length) return '';
  const number = Number(index);
  return Number.isInteger(number) && number >= 0 && number < snippets.length ? String(number) : '0';
}
function showToast(message, tone = 'info') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.dataset.tone = tone;
  toast.classList.add('is-visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('is-visible'), 2400);
}
function showCompetenceToast(key, message, tone = 'info') {
  if (lastCompetenceToastKey === key) return;
  lastCompetenceToastKey = key;
  showToast(message, tone);
}
function renderMobileBar() {
  const bar = document.getElementById('mobile-review-bar');
  const sheet = document.getElementById('mobile-sheet');
  const sheetContent = document.getElementById('mobile-sheet-content');
  const run = currentRun();
  if (!bar || !run) return;
  const entry = currentFileEntry(run);
  const total = (run.outputs || []).length;
  bar.innerHTML = `<button id="mobile-prev" class="icon-command" aria-label="Previous file" ${entry && entry.index > 0 ? '' : 'disabled'}>${iconArrowLeft()}</button><span class="mobile-status">${entry ? `${entry.index + 1} of ${total}` : escapeHtml(labelForStatus(reviewStatus(run.id)))}</span><button id="mobile-complete" class="quiet-command">${iconCheck()}<span>Complete</span></button><button id="mobile-next" class="icon-command" aria-label="Next file" ${entry && entry.index < total - 1 ? '' : 'disabled'}>${iconArrowRight()}</button><button id="mobile-open-sheet" class="mobile-details-action">${iconMenu()}<span>Details</span></button>`;
  sheet.classList.toggle('is-open', state.mobileSheetOpen);
  sheet.setAttribute('aria-hidden', state.mobileSheetOpen ? 'false' : 'true');
  sheetContent.innerHTML = state.mobileSheetOpen ? renderMobileSheetContent() : '';
}
function renderMobileSheetContent() {
  const source = document.getElementById('review-dock-content');
  if (!source) return '';
  const clone = source.cloneNode(true);
  const idMap = new Map();
  clone.querySelectorAll('[id]').forEach(el => {
    const oldId = el.id;
    const nextId = `mobile-${oldId}`;
    idMap.set(oldId, nextId);
    el.dataset.mobileOriginalId = oldId;
    el.id = nextId;
  });
  clone.querySelectorAll('[for], [aria-labelledby], [aria-controls], [aria-describedby]').forEach(el => {
    for (const attrName of ['for', 'aria-labelledby', 'aria-controls', 'aria-describedby']) {
      const value = el.getAttribute(attrName);
      if (!value) continue;
      el.setAttribute(attrName, value.split(/\s+/).map(id => idMap.get(id) || id).join(' '));
    }
  });
  return clone.innerHTML;
}
function runV11SelfCheck() {
  const results = [];
  const add = (name, pass, detail = '') => results.push({ name, pass: Boolean(pass), detail });
  const control = data.runs.find(run => run.id === 'iter-8-control');
  const baseline = data.runs.find(run => run.id === 'iter-8-baseline');
  const regression = data.runs.find(run => run.id === 'iter-8-regression-check');
  add('control requires previous context', requirementsForRun(control).some(req => req.key === 'previous_output_checked'));
  add('baseline does not require previous context', !requirementsForRun(baseline).some(req => req.key === 'previous_output_checked'));
  add('regression requires previous context', requirementsForRun(regression).some(req => req.key === 'previous_output_checked'));
  add('baseline has failed grades', failedGradeCount(baseline) > 0);
  add('baseline blockers omit previous context', !completionIssues(baseline, { includeQuality: true }).some(issue => issue.requirement === 'previous_output_checked'));
  add('control blockers include previous context', completionIssues(control, { includeQuality: true }).some(issue => issue.requirement === 'previous_output_checked'));
  const secretRun = { id: 'secret-check', outputs: [{ name: 'out.txt', type: 'text', content: 'ok' }], grading: null };
  data.runs.push(secretRun);
  const secretKey = outputKey(secretRun.id, secretRun.outputs[0], 0);
  const secretReview = fileReviewForKey(secretKey);
  secretReview.inspected = true;
  secretReview.decision = 'accepted';
  secretReview.note = `token ${'sk'}-${'testsecretvalue1234567890'}`;
  syncOutputChecklist(secretRun);
  add('sensitive feedback blocks export', completionIssues(secretRun, { includeQuality: true }).some(issue => issue.severity === 'blocker' && issue.type === 'sensitive'));
  data.runs.pop();
  delete state.fileReviews[secretKey];
  delete state.checklist[secretRun.id];
  const migrated = normalizeData({ schemaVersion: 'eval-review-7.0', source: { skill_name: 'old', runs: [{ id: 'old-run', output: 'ok' }] } });
  add('retired bundle source migrates', migrated.runs.length === 1 && migrated.runs[0].id === 'old-run');
  return { passed: results.filter(result => result.pass).length, total: results.length, results };
}
window.runV11SelfCheck = runV11SelfCheck;
function openMobileSheet() {
  lastModalFocus = document.activeElement;
  state.mobileSheetOpen = true;
  render();
  setTimeout(() => document.querySelector('#mobile-sheet .mobile-sheet-panel')?.focus(), 0);
}
function wireEvents() {
  document.getElementById('btn-finalize').addEventListener('click', () => submitFeedback());
  document.getElementById('btn-export-bundle').addEventListener('click', exportBundle);
  document.getElementById('btn-export-md').addEventListener('click', exportMarkdown);
  document.getElementById('btn-theme').addEventListener('click', () => { state.theme = state.theme === 'dark' ? 'light' : 'dark'; render(); });
  document.getElementById('btn-reset').addEventListener('click', resetReview);
  document.getElementById('btn-mobile-details-menu')?.addEventListener('click', openMobileSheet);
  document.getElementById('btn-bulk-approve')?.addEventListener('click', approveVisible);
  document.getElementById('btn-close-submit').addEventListener('click', closeSubmitGate);
  document.getElementById('btn-submit-override').addEventListener('click', () => { closeSubmitGate(); submitFeedback({ force: true }); });
  document.getElementById('btn-close-bulk').addEventListener('click', closeBulkGate);
  document.getElementById('btn-bulk-confirm').addEventListener('click', confirmBulkApprove);
  document.getElementById('btn-close-mobile-sheet').addEventListener('click', () => { state.mobileSheetOpen = false; render(); lastModalFocus?.focus?.(); });
  document.addEventListener('click', handleDocumentClick);
  document.addEventListener('input', handleDocumentInput);
  document.addEventListener('change', handleDocumentChange);
  document.addEventListener('keydown', handleDocumentKeydown);
  document.addEventListener('mouseover', handleTooltipShow);
  document.addEventListener('focusin', handleTooltipShow);
  document.addEventListener('mouseout', handleTooltipHide);
  document.addEventListener('focusout', handleTooltipHide);
}
function handleTooltipShow(event) {
  const target = event.target.closest?.('[data-tooltip]');
  if (!target) return;
  const bubble = document.getElementById('tooltip-bubble');
  if (!bubble) return;
  bubble.textContent = target.dataset.tooltip || '';
  const rect = target.getBoundingClientRect();
  bubble.style.left = `${rect.left + rect.width / 2}px`;
  bubble.style.top = `${rect.bottom + 8}px`;
  bubble.classList.toggle('is-visible', Boolean(bubble.textContent));
}
function handleTooltipHide(event) {
  if (!event.target.closest?.('[data-tooltip]')) return;
  document.getElementById('tooltip-bubble')?.classList.remove('is-visible');
}
function handleDocumentClick(event) {
  document.getElementById('tooltip-bubble')?.classList.remove('is-visible');
  const issueRow = event.target.closest('[data-issue-index]');
  if (issueRow) {
    const modal = document.getElementById('submit-modal');
    const issue = modal._issues?.[Number(issueRow.dataset.issueIndex)];
    closeSubmitGate();
    if (issue) focusIssue(issue);
    return;
  }
  const decision = event.target.closest('[data-decision]');
  if (decision) {
    applyDecision(decision.dataset.decision);
    return;
  }
  if (event.target.closest('[data-complete-current]')) {
    completeCurrentRun({ advance: false });
    return;
  }
  if (event.target.closest('#mobile-complete')) {
    completeCurrentRun({ advance: false });
    return;
  }
  if (event.target.closest('#mobile-prev')) { navigateFile(-1); return; }
  if (event.target.closest('#mobile-next')) { navigateFile(1); return; }
  if (event.target.closest('#mobile-open-sheet')) { openMobileSheet(); return; }
  if (event.target.closest('[data-focus-files]')) {
    state.tab = 'review';
    render();
    focusOutputFileReview({ type: 'checklist:output_inspected' });
    return;
  }
  const fileTemplate = event.target.closest('[data-file-template]');
  if (fileTemplate) {
    applyFileFeedbackTemplate(fileTemplate.dataset.fileTemplate || '', fileTemplate.dataset.fileTemplateDecision || 'unreviewed');
    return;
  }
  const fileNav = event.target.closest('[data-file-nav]');
  if (fileNav) {
    navigateFile(fileNav.dataset.fileNav === 'prev' ? -1 : 1);
    return;
  }
  if (event.target.closest('[data-random-file]')) {
    chooseRandomFile();
    return;
  }
  if (event.target.closest('[data-random-run]')) {
    chooseRandomRun();
    return;
  }
  if (event.target.closest('[data-file-bookmark]')) {
    toggleFileBookmark();
    return;
  }
  if (event.target.closest('#btn-run-bookmark')) {
    toggleBookmark();
    return;
  }
  const copyOutput = event.target.closest('[data-copy-output]');
  if (copyOutput) {
    const file = fileForKey(copyOutput.dataset.copyOutput);
    if (file) copyText(outputText(file));
    return;
  }
  const openOutput = event.target.closest('[data-open-output]');
  if (openOutput) {
    const file = fileForKey(openOutput.dataset.openOutput);
    if (file?.data_uri) window.open(file.data_uri, '_blank', 'noopener');
    return;
  }
  if (event.target.closest('#btn-copy-feedback')) {
    copyText(document.getElementById('feedback-preview-json')?.textContent || JSON.stringify(buildFeedbackPayload('in_progress'), null, 2));
    return;
  }
  if (event.target.closest('#btn-download-feedback-preview')) {
    downloadFeedbackPreview();
    return;
  }
  const feedbackRun = event.target.closest('[data-feedback-run-index]');
  if (feedbackRun) {
    saveCurrentFeedback();
    state.currentIndex = Number(feedbackRun.dataset.feedbackRunIndex);
    const run = currentRun();
    const firstIssue = run ? qaIssues(run)[0] : null;
    if (firstIssue && feedbackRun.dataset.feedbackFix != null) focusIssue(firstIssue);
    else { state.tab = 'review'; render(); document.getElementById('main')?.focus(); }
    return;
  }
  const diffMode = event.target.closest('[data-diff-mode]');
  if (diffMode) {
    state.comparisonMode = diffMode.dataset.diffMode;
    renderMain();
    persistSoon();
    return;
  }
  const artifactToggle = event.target.closest('[data-artifacts-toggle]');
  if (artifactToggle) {
    const run = currentRun();
    const expandAll = !allOutputsExpanded(run);
    for (const [index, file] of (run?.outputs || []).entries()) {
      state.collapsedFiles[outputKey(run.id, file, index)] = !expandAll;
    }
    renderMain();
    persistSoon();
    return;
  }
  if (event.target.closest('#btn-undo-bulk')) {
    undoBulkApprove();
    return;
  }
  if (event.target.closest('#btn-bulk-approve')) {
    approveVisible();
    return;
  }
  const dropdownOption = event.target.closest('[data-dropdown-value]');
  if (dropdownOption) {
    const dropdown = dropdownOption.closest('[data-dropdown]');
    if (dropdown) handleDropdownSelection(dropdown.dataset.dropdown, dropdownOption.dataset.dropdownValue);
    return;
  }
  const bookmarkFilter = event.target.closest('[data-filter-bookmarked]');
  if (bookmarkFilter) {
    saveCurrentFeedback();
    state.bookmarkedOnly = !state.bookmarkedOnly;
    renderFilterControl();
    renderRunList();
    persistSoon();
    return;
  }
  if (event.target.closest('[data-clear-filters]')) {
    saveCurrentFeedback();
    state.flowFilter = 'all';
    state.statusFilter = 'all';
    state.severityFilter = 'all';
    state.bookmarkedOnly = false;
    renderFilterControl();
    renderRunList();
    persistSoon();
    return;
  }
  const flowToggle = event.target.closest('[data-flow-toggle]');
  if (flowToggle) {
    saveCurrentFeedback();
    const flow = state.flowFilter || 'all';
    state.flowFilter = flow === 'open' ? 'complete' : 'open';
    if (state.statusFilter === 'open' || state.statusFilter === 'complete') state.statusFilter = 'all';
    renderFilterControl();
    renderRunList();
    persistSoon();
    return;
  }
  const sortDirection = event.target.closest('[data-sort-direction]');
  if (sortDirection) {
    saveCurrentFeedback();
    state.sortDirection = state.sortDirection === 'desc' ? 'asc' : 'desc';
    renderFilterControl();
    renderRunList();
    persistSoon();
    return;
  }
  const dropdownTrigger = event.target.closest('[data-dropdown-trigger]');
  if (dropdownTrigger) {
    const dropdown = dropdownTrigger.closest('[data-dropdown]');
    if (dropdown) toggleDropdown(dropdown);
    return;
  }
  if (!event.target.closest('.dropdown')) closeDropdowns();
  const snippetSummary = event.target.closest('.snippet-details > summary');
  if (snippetSummary) {
    setTimeout(() => {
      state.snippetsOpen = snippetSummary.parentElement.open;
      persistSoon();
    }, 0);
  }

  const runButton = event.target.closest('[data-run-index]');
  if (runButton) {
    saveCurrentFeedback();
    state.currentIndex = Number(runButton.dataset.runIndex);
    render();
    document.getElementById('main').focus();
    return;
  }
  const tab = event.target.closest('[data-tab]');
  if (tab) {
    saveCurrentFeedback();
    state.tab = tab.dataset.tab;
    render();
    return;
  }
  const expand = event.target.closest('[data-output-expand]');
  if (expand) {
    state.expandedOutputs[expand.dataset.outputExpand] = !state.expandedOutputs[expand.dataset.outputExpand];
    renderMain();
    persistSoon();
    return;
  }
  const fileToggle = event.target.closest('[data-file-toggle]');
  if (fileToggle) {
    const key = fileToggle.dataset.fileToggle;
    state.collapsedFiles[key] = fileToggle.getAttribute('aria-expanded') === 'true';
    renderMain();
    persistSoon();
    return;
  }
  if (event.target.closest('#btn-prev')) navigateFile(-1);
  if (event.target.closest('#btn-next')) navigateFile(1);
  if (event.target.closest('#btn-bookmark')) toggleFileBookmark();
  const snippetRoot = event.target.closest('.dock-details, .mobile-sheet-panel') || document;
  if (event.target.closest('[data-snippet-apply], #btn-apply-snippet')) {
    const index = normalizeSnippetIndex(state.activeSnippetIndex);
    const snippet = index === '' ? '' : state.snippets[Number(index)];
    if (snippet) applySnippet(snippet);
  }
  if (event.target.closest('[data-snippet-save], #btn-save-snippet')) {
    const input = snippetRoot.querySelector('[data-snippet-text], #snippet-text');
    const text = input?.value.trim();
    if (text) {
      const existing = state.snippets.indexOf(text);
      if (existing >= 0) {
        state.activeSnippetIndex = String(existing);
      } else {
        state.snippets.push(text);
        state.activeSnippetIndex = String(state.snippets.length - 1);
      }
      state.snippetsOpen = true;
      renderReviewDock();
      renderMobileBar();
      persistSoon();
    }
  }
  if (event.target.closest('[data-snippet-delete], #btn-delete-snippet')) {
    const index = normalizeSnippetIndex(state.activeSnippetIndex);
    if (index !== '') {
      state.snippets.splice(Number(index), 1);
      state.activeSnippetIndex = normalizeSnippetIndex(Number(index), state.snippets);
      state.snippetsOpen = true;
      renderReviewDock();
      renderMobileBar();
      persistSoon();
    }
  }
}
function toggleDropdown(dropdown) {
  const willOpen = !dropdown.classList.contains('is-open');
  closeDropdowns(dropdown);
  dropdown.classList.toggle('is-open', willOpen);
  dropdown.querySelector('[data-dropdown-trigger]')?.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
}
function closeDropdowns(except = null) {
  document.querySelectorAll('.dropdown.is-open').forEach(dropdown => {
    if (dropdown === except) return;
    dropdown.classList.remove('is-open');
    dropdown.querySelector('[data-dropdown-trigger]')?.setAttribute('aria-expanded', 'false');
  });
}
function handleDropdownKeys(event, dropdown) {
  const codes = ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', 'Escape'];
  if (!codes.includes(event.code)) return false;
  event.preventDefault();
  const trigger = dropdown.querySelector('[data-dropdown-trigger]');
  const options = Array.from(dropdown.querySelectorAll('[data-dropdown-value]'));
  if (event.code === 'Escape') { closeDropdowns(); trigger?.focus(); return true; }
  if (!dropdown.classList.contains('is-open')) toggleDropdown(dropdown);
  const current = Math.max(0, options.indexOf(document.activeElement));
  let next = current;
  if (event.code === 'ArrowDown') next = Math.min(options.length - 1, current + 1);
  if (event.code === 'ArrowUp') next = Math.max(0, current - 1);
  if (event.code === 'Home') next = 0;
  if (event.code === 'End') next = options.length - 1;
  if (event.code === 'Enter' && document.activeElement?.dataset?.dropdownValue) {
    handleDropdownSelection(dropdown.dataset.dropdown, document.activeElement.dataset.dropdownValue);
    closeDropdowns();
    trigger?.focus();
    return true;
  }
  options[next]?.focus();
  return true;
}
function handleDropdownSelection(id, value) {
  closeDropdowns();
  const run = currentRun();
  if (id === 'filter-status') {
    state.statusFilter = value;
    renderFilterControl();
    renderRunList();
    persistSoon();
    return;
  }
  if (id === 'filter-severity') {
    state.severityFilter = value;
    renderFilterControl();
    renderRunList();
    persistSoon();
    return;
  }
  if (id === 'file-decision' && run) {
    applyFileDecision(value);
    return;
  }
  if (id === 'file-severity' && run) {
    applyFileSeverity(value);
    return;
  }
  if (id === 'review-status' && run) {
    state.status[run.id] = value;
    checklistFor(run.id).feedback_decision = value !== 'unreviewed';
    renderReviewDock();
    renderChrome();
    renderRunList();
    persistSoon();
    return;
  }
  if (id === 'review-severity' && run) {
    state.severity[run.id] = value;
    renderReviewDock();
    renderChrome();
    renderRunList();
    persistSoon();
    return;
  }
  if (id === 'snippet-select') {
    state.activeSnippetIndex = normalizeSnippetIndex(value);
    renderReviewDock();
    renderMobileBar();
    persistSoon();
  }
}
function handleDocumentInput(event) {
  const run = currentRun();
  if (event.target.id === 'search') {
    state.search = event.target.value;
    renderRunList();
    persistSoon();
  }
  if (event.target.id === 'feedback' && run) {
    state.feedback[run.id] = event.target.value;
    checklistFor(run.id).feedback_decision = Boolean(event.target.value.trim()) || reviewStatus(run.id) !== 'unreviewed';
    renderFeedbackQualityOnly(event.target.value, event.target);
    renderReadinessOnly();
    persistSoon();
  }
  if (event.target.matches('[data-file-note]') && run) {
    const review = fileReviewForKey(event.target.dataset.fileNote);
    review.note = event.target.value;
    state.timestamps[run.id] = state.timestamps[run.id] || new Date().toISOString();
    syncOutputChecklist(run);
    if (['issue', 'blocked'].includes(review.decision) && review.note.trim().length >= 12) showCompetenceToast(`${event.target.dataset.fileNote}:note`, review.decision === 'issue' ? 'Issue captured with note' : 'Blocked file captured', 'good');
    renderFeedbackQualityOnly(event.target.value, event.target);
    persistSoon();
  }
}
function renderFeedbackQualityOnly(value, source = null) {
  const warnings = [...feedbackQualityWarnings(value), ...secretWarnings(value)];
  if (source?.matches?.('[data-file-note]')) {
    const field = source.closest('.file-review-note, .feedback-field');
    let el = field?.querySelector('.feedback-quality');
    if (!el && warnings.length && field) {
      el = document.createElement('div');
      el.className = 'feedback-quality';
      field.appendChild(el);
    }
    if (el) {
      el.classList.toggle('is-warning', warnings.length > 0);
      el.innerHTML = renderFeedbackQuality(warnings);
    }
    source.classList.toggle('is-invalid', warnings.length > 0);
    source.setAttribute('aria-invalid', warnings.length ? 'true' : 'false');
    return;
  }
  const root = source?.closest?.('.dock-card, .mobile-sheet-panel') || document;
  const el = root.querySelector?.('#feedback-quality') || document.getElementById('feedback-quality');
  const input = root.querySelector?.('#feedback') || activeControl('feedback');
  if (el) {
    el.classList.toggle('is-warning', warnings.length > 0);
    el.innerHTML = renderFeedbackQuality(warnings);
  }
  if (input) {
    input.classList.toggle('is-invalid', warnings.length > 0);
    input.setAttribute('aria-invalid', warnings.length ? 'true' : 'false');
  }
}
function handleDocumentChange(event) {
  const run = currentRun();
  if (event.target.id === 'filter') {
    state.filter = event.target.value;
    renderRunList();
    persistSoon();
  }
  if (event.target.id === 'review-status' && run) {
    state.status[run.id] = event.target.value;
    checklistFor(run.id).feedback_decision = event.target.value !== 'unreviewed';
    renderReadinessOnly();
    persistSoon();
  }
  if (event.target.id === 'review-severity' && run) {
    state.severity[run.id] = event.target.value;
    renderReadinessOnly();
    persistSoon();
  }
  if (event.target.matches('[data-checklist]') && run) {
    checklistFor(run.id)[event.target.dataset.checklist] = event.target.checked;
    renderReadinessOnly();
    persistSoon();
  }
  if (event.target.matches('[data-file-decision]') && run) {
    applyFileDecision(event.target.value);
  }
  if (event.target.id === 'bulk-passing-only') {
    document.getElementById('bulk-confirm-count').value = '';
    renderBulkGateMessage();
  }
}
function handleDocumentKeydown(event) {
  if (trapModalFocus(event)) return;
  if (event.code === 'Escape') {
    if (document.getElementById('submit-modal').classList.contains('is-open')) closeSubmitGate();
    else if (document.getElementById('bulk-modal').classList.contains('is-open')) closeBulkGate();
    else if (state.mobileSheetOpen) { state.mobileSheetOpen = false; render(); lastModalFocus?.focus?.(); }
    else closeDropdowns();
    return;
  }
  const activeDropdown = event.target.closest?.('[data-dropdown]');
  if (activeDropdown && handleDropdownKeys(event, activeDropdown)) return;
  const tag = document.activeElement?.tagName;
  const editing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || document.activeElement?.isContentEditable;
  if ((event.getModifierState('Control') || event.getModifierState('Meta')) && event.code === 'Enter') {
    event.preventDefault();
    completeCurrentRun();
    return;
  }
  if (editing) return;
  if (event.code === 'ArrowRight' || event.code === 'KeyL') { event.preventDefault(); navigateFile(1); }
  else if (event.code === 'ArrowLeft' || event.code === 'KeyH') { event.preventDefault(); navigateFile(-1); }
  else if (event.code === 'KeyG') { event.preventDefault(); state.tab = 'review'; render(); document.getElementById('grades-section')?.focus(); }
  else if (event.code === 'KeyP') { event.preventDefault(); state.tab = 'review'; render(); document.getElementById('previous-context-section')?.focus(); }
  else if (event.code === 'KeyB') { event.preventDefault(); state.tab = 'benchmark'; render(); }
}
wireEvents();
render();
