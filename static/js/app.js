const STORAGE_KEY = "skillup.frontend.state.v3";
const GAP_INTERVAL = 2000;
const GAP_MAX_POLLS = 60;

const defaults = {
    activeStage: "discover-stage",
    sessionId: null,
    targetRole: "Backend Developer",
    resumeSummary: null,
    trendingSkills: [],
    skillGapJobId: null,
    skillGapResult: null,
    recommendedSkills: [],
    selectedSkill: null,
    declaredLevel: "Intermediate",
    assessmentQuestions: [],
    assessmentAnswers: [],
    validatedLevel: null,
    topics: [],
    knownTopics: [],
    excludedTopics: [],
    selectedTopic: null,
    preferredDuration: "40 min",
    videos: [],
    selectedVideo: null,
    scheduleChoice: "none",
    scheduleMode: "balanced",
    schedule: null,
    executionConfig: null,
    currentLanguage: null,
    starterCode: "",
    code: "",
    practicePack: null,
    practiceAnswers: [],
    practiceEvaluation: null,
    hintLevel: 1,
    hint: null,
    evaluationPack: null,
    evaluationAnswers: [],
    evaluationResult: null,
    jobs: [],
    validationOutput: "Validation output will appear here.",
    executionOutput: "Execution output will appear here."
};

const state = { ...defaults };
const busy = {};
const gapPhases = [
    "Preparing market profile...",
    "Fetching live jobs...",
    "Extracting market skills...",
    "Comparing against your profile..."
];

let els = {};
let gapTimer = null;

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

const fmt = value => value ? String(value).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()) : "Not set";
const learnLevel = () => state.validatedLevel?.validated_level || state.declaredLevel;
const hasValidatedLevel = () => Boolean(state.validatedLevel?.validated_level || state.validatedLevel?.validatedLevel || state.validatedLevel);
const toggleEmpty = (node, empty) => node && node.classList.toggle("empty-state", empty);
const friendlyError = error => {
    const message = String(error?.message || error || "Unknown error");
    if (/no response from tool/i.test(message)) {
        return "The evaluator did not return in time.";
    }
    return message;
};

function save() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
        activeStage: state.activeStage,
        sessionId: state.sessionId,
        targetRole: state.targetRole,
        resumeSummary: state.resumeSummary,
        trendingSkills: state.trendingSkills,
        skillGapResult: state.skillGapResult,
        recommendedSkills: state.recommendedSkills,
        selectedSkill: state.selectedSkill,
        declaredLevel: state.declaredLevel,
        assessmentQuestions: state.assessmentQuestions,
        assessmentAnswers: state.assessmentAnswers,
        validatedLevel: state.validatedLevel,
        topics: state.topics,
        knownTopics: state.knownTopics,
        excludedTopics: state.excludedTopics,
        selectedTopic: state.selectedTopic,
        preferredDuration: state.preferredDuration,
        videos: state.videos,
        selectedVideo: state.selectedVideo,
        scheduleChoice: state.scheduleChoice,
        scheduleMode: state.scheduleMode,
        schedule: state.schedule,
        currentLanguage: state.currentLanguage,
        starterCode: state.starterCode,
        code: state.code,
        practicePack: state.practicePack,
        practiceAnswers: state.practiceAnswers,
        practiceEvaluation: state.practiceEvaluation,
        hintLevel: state.hintLevel,
        hint: state.hint,
        evaluationPack: state.evaluationPack,
        evaluationAnswers: state.evaluationAnswers,
        evaluationResult: state.evaluationResult,
        jobs: state.jobs
    }));
}

function load() {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    try {
        Object.assign(state, defaults, JSON.parse(raw));
    } catch (_) { }
}

function toast(title, message, tone = "default") {
    const item = document.createElement("div");
    item.className = "toast";
    item.dataset.state = tone;
    item.innerHTML = `<span class="toast-title">${esc(title)}</span><p class="toast-message">${esc(message)}</p>`;
    els.toastArea.appendChild(item);
    setTimeout(() => item.remove(), 4200);
}

function setStatus(node, message, tone = "default") {
    if (!node) return;
    node.textContent = message;
    node.dataset.state = tone;
}

async function api(url, options = {}) {
    const opts = { method: "GET", ...options };
    if (opts.body && !(opts.body instanceof FormData)) {
        opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
        opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(url, opts);
    const text = await response.text();
    let payload = {};
    if (text) {
        try {
            payload = JSON.parse(text);
        } catch (_) {
            payload = { raw: text };
        }
    }
    if (!response.ok) throw new Error(payload.error || payload.message || payload.raw || `Request failed (${response.status})`);
    if (payload.success === false) throw new Error(payload.error || "Request failed");
    const data = Object.prototype.hasOwnProperty.call(payload, "success") ? payload.data : payload;
    if (data && typeof data === "object" && !Array.isArray(data) && typeof data.error === "string" && data.error.trim()) {
        throw new Error(data.error);
    }
    return data;
}

async function task(name, button, fn) {
    if (busy[name]) return;
    busy[name] = true;
    if (button) button.disabled = true;
    try {
        return await fn();
    } finally {
        busy[name] = false;
        if (button) button.disabled = false;
    }
}

function capture() {
    els = {
        toastArea: $("toast-area"),
        introPanel: document.querySelector(".intro-panel"),
        stageSections: Array.from(document.querySelectorAll(".stage-section")),
        stageLinks: Array.from(document.querySelectorAll("[data-stage-link]")),
        sessionPill: $("session-pill"),
        startSessionBtn: $("start-session-btn"),
        restoreResultBtn: $("restore-result-btn"),
        loadScheduleBtn: $("load-schedule-btn"),
        summarySession: $("summary-session"),
        summaryRole: $("summary-role"),
        summarySkill: $("summary-skill"),
        summaryLevel: $("summary-level"),
        summaryTopic: $("summary-topic"),
        summarySchedule: $("summary-schedule"),
        summaryEval: $("summary-eval"),
        targetRoleInput: $("target-role-input"),
        resumeFile: $("resume-file"),
        resumeFileName: $("resume-file-name"),
        analyzeResumeBtn: $("analyze-resume-btn"),
        fetchTrendingBtn: $("fetch-trending-btn"),
        startSkillGapBtn: $("start-skill-gap-btn"),
        discoverStatus: $("discover-status"),
        resumeSummary: $("resume-summary"),
        trendingMeta: $("trending-meta"),
        trendingSkills: $("trending-skills"),
        skillGapStatus: $("skill-gap-status"),
        skillGapResult: $("skill-gap-result"),
        recommendedSkills: $("recommended-skills"),
        manualSkillInput: $("manual-skill-input"),
        useManualSkillBtn: $("use-manual-skill-btn"),
        assessmentSkillPill: $("assessment-skill-pill"),
        generateAssessmentBtn: $("generate-assessment-btn"),
        assessmentQuestions: $("assessment-questions"),
        validateLevelBtn: $("validate-level-btn"),
        validatedLevelResult: $("validated-level-result"),
        fetchTopicsBtn: $("fetch-topics-btn"),
        topicsStatus: $("topics-status"),
        topicsList: $("topics-list"),
        videosPanel: $("videos-panel"),
        dailyTimeInput: $("daily-time-input"),
        scheduleControls: $("schedule-controls"),
        scheduleView: $("schedule-view"),
        generateScheduleBtn: $("generate-schedule-btn"),
        completeDayBtn: $("complete-day-btn"),
        fetchVideosBtn: $("fetch-videos-btn"),
        videosMeta: $("videos-meta"),
        videosList: $("videos-list"),
        practicePanel: $("practice-panel"),
        languageSelect: $("language-select"),
        resetCodeBtn: $("reset-code-btn"),
        codeSkillLabel: $("code-skill-label"),
        codeTopicLabel: $("code-topic-label"),
        codeVideoLabel: $("code-video-label"),
        runtimeNote: $("runtime-note"),
        codePanel: $("code-panel"),
        codeTextarea: $("code-textarea"),
        stdinInput: $("stdin-input"),
        validateCodeBtn: $("validate-code-btn"),
        runCodeBtn: $("run-code-btn"),
        validationOutput: $("validation-output"),
        executionOutput: $("execution-output"),
        generatePracticeBtn: $("generate-practice-btn"),
        evaluatePracticeBtn: $("evaluate-practice-btn"),
        practiceSummary: $("practice-summary"),
        practiceQuestions: $("practice-questions"),
        miniLab: $("mini-lab"),
        practiceEvaluation: $("practice-evaluation"),
        requestHintBtn: $("request-hint-btn"),
        hintOutput: $("hint-output"),
        generateEvaluationBtn: $("generate-evaluation-btn"),
        scoreEvaluationBtn: $("score-evaluation-btn"),
        evaluationMeta: $("evaluation-meta"),
        evaluationPanel: $("evaluation-panel"),
        evaluationPack: $("evaluation-pack"),
        evaluationResult: $("evaluation-result"),
        fetchJobsBtn: $("fetch-jobs-btn"),
        jobsList: $("jobs-list")
    };
}

function setInitialSelections() {
    document.querySelectorAll("[data-role-chip]").forEach(btn => btn.classList.toggle("chip-active", btn.dataset.roleChip === state.targetRole));
    document.querySelectorAll("[data-level]").forEach(btn => btn.classList.toggle("level-pill-active", btn.dataset.level === state.declaredLevel));
    document.querySelectorAll("[data-duration]").forEach(btn => btn.classList.toggle("mode-pill-active", btn.dataset.duration === state.preferredDuration));
    document.querySelectorAll("[data-hint-level]").forEach(btn => btn.classList.toggle("mode-pill-active", Number(btn.dataset.hintLevel) === Number(state.hintLevel)));
    document.querySelectorAll("[data-schedule-toggle]").forEach(btn => btn.classList.toggle("mode-pill-active", btn.dataset.scheduleToggle === state.scheduleChoice));
    document.querySelectorAll("[data-schedule-mode]").forEach(btn => btn.classList.toggle("mode-pill-active", btn.dataset.scheduleMode === state.scheduleMode));
}

function updateSummary() {
    els.summarySession.textContent = state.sessionId || "Not started";
    els.summaryRole.textContent = state.targetRole || "Not set";
    els.summarySkill.textContent = state.selectedSkill || "Not selected";
    els.summaryLevel.textContent = hasValidatedLevel() ? (state.validatedLevel?.validated_level || state.validatedLevel?.validatedLevel || state.validatedLevel) : (state.selectedSkill ? "Pending" : "Not selected");
    els.summaryTopic.textContent = state.selectedTopic || "Pending";
    els.summarySchedule.textContent = state.schedule?.mode ? fmt(state.schedule.mode) : (state.scheduleChoice === "create" ? "Pending" : "No schedule");
    els.summaryEval.textContent = state.evaluationResult?.readiness || (state.evaluationPack ? "Questions ready" : "Not started");
    els.sessionPill.textContent = state.sessionId ? `Session ${state.sessionId}` : "Session idle";
    els.startSessionBtn.textContent = state.sessionId ? "New Session" : "Start Session";
    els.assessmentSkillPill.textContent = state.selectedSkill || "No skill selected";
    els.codeSkillLabel.textContent = state.selectedSkill || "Not selected";
    els.codeTopicLabel.textContent = state.selectedTopic || "Not selected";
    els.codeVideoLabel.textContent = state.selectedVideo?.title || "None";
    save();
}

function setActiveStage(stageId, syncHash = true) {
    const next = ["discover-stage", "learn-stage", "prove-stage"].includes(stageId) ? stageId : "discover-stage";
    state.activeStage = next;
    els.stageSections.forEach(section => section.classList.toggle("is-hidden", section.id !== next));
    if (els.introPanel) els.introPanel.classList.toggle("is-hidden", next !== "discover-stage");
    els.stageLinks.forEach(link => {
        const active = link.dataset.stageLink === next;
        link.classList.toggle("stage-link-active", active && link.classList.contains("stage-link"));
        link.classList.toggle("mobile-nav-link-active", active && link.classList.contains("mobile-nav-link"));
    });
    if (syncHash && window.location.hash !== `#${next}`) {
        history.replaceState(null, "", `#${next}`);
    }
    save();
}

function requireValidatedSkill(message, stageId = "discover-stage") {
    if (!state.selectedSkill) {
        toast("Skill required", "Select a skill first in Agent 1.", "error");
        setActiveStage(stageId);
        return false;
    }
    if (!hasValidatedLevel()) {
        setStatus(els.discoverStatus, message || "Validate the selected skill level in Agent 1 before continuing.", "warning");
        toast("Validation required", "Complete Agent 1 level validation before continuing.", "error");
        setActiveStage(stageId);
        return false;
    }
    return true;
}

function tagList(items, tone = "") {
    if (!items || !items.length) return `<span class="tag">None yet</span>`;
    return items.map(item => `<span class="tag ${tone}">${esc(item)}</span>`).join("");
}

function questionMarkup(question, answer, index, prefix) {
    const options = Array.isArray(question.options) && question.options.length
        ? `<div class="option-list">${question.options.map(option => `<label class="option-item"><input type="radio" name="${prefix}-${index}" value="${esc(option)}" ${answer === option ? "checked" : ""} data-${prefix}-index="${index}"><span>${esc(option)}</span></label>`).join("")}</div>`
        : `<div class="question-answer"><textarea data-${prefix}-index="${index}" placeholder="Write a short answer.">${esc(answer)}</textarea></div>`;
    return `<article class="question-card"><p class="question-meta">Question ${index + 1}</p><h4 class="question-title">${esc(question.question || question.prompt || "Question")}</h4>${question.real_world_context ? `<p class="card-body">${esc(question.real_world_context)}</p>` : ""}${options}</article>`;
}

function renderResume() {
    const data = state.resumeSummary;
    toggleEmpty(els.resumeSummary, !data);
    els.resumeSummary.innerHTML = !data ? "Upload a resume to extract skills, experience level, and domain." : `<div class="results-stack"><div class="mini-kv"><div class="mini-kv-item"><span class="summary-label">Experience</span><strong>${esc(data.experience_level || "Unknown")}</strong></div><div class="mini-kv-item"><span class="summary-label">Domain</span><strong>${esc(data.domain || "Unknown")}</strong></div></div><div><span class="summary-label">Extracted Skills</span><div class="tag-row">${tagList(data.user_skills, "tag-primary")}</div></div></div>`;
}

function renderTrending() {
    toggleEmpty(els.trendingSkills, !state.trendingSkills.length);
    els.trendingSkills.innerHTML = !state.trendingSkills.length ? "Fetch trending skills to see role-relevant market demand." : state.trendingSkills.map(item => `<article class="trending-card"><div class="card-header"><div><p class="card-meta">Role-based signal</p><h4 class="card-headline">${esc(item.skill || item.name || "Skill")}</h4></div><span class="tag tag-secondary">${esc(item.demand || item.urgency || "Live")}</span></div><p class="card-body">${esc(item.reason || "Current market demand for this role.")}</p></article>`).join("");
}

function renderGap() {
    if (!state.skillGapResult) {
        toggleEmpty(els.skillGapResult, true);
        els.skillGapResult.innerHTML = "No skill-gap result yet.";
        return;
    }
    toggleEmpty(els.skillGapResult, false);
    const data = state.skillGapResult.result || state.skillGapResult;
    state.recommendedSkills = Array.isArray(data.recommended_skills) ? data.recommended_skills : [];
    if (Array.isArray(data.trending_skills) && data.trending_skills.length) state.trendingSkills = data.trending_skills;
    els.skillGapResult.innerHTML = `<div class="results-stack"><div class="result-card"><p class="result-meta">${esc(state.targetRole || data.target_role || "Target role")}</p><h4 class="result-title">${esc(data.role_context || "Profile compared against live market demand.")}</h4><div class="tag-row"><span class="tag ${data.warm || data.market_profile_warm ? "tag-primary" : "tag-secondary"}">${data.warm || data.market_profile_warm ? "Market profile warm" : "Fresh market pull"}</span><span class="tag">${esc(`${(data.skill_gaps || []).length} gaps`)}</span><span class="tag">${esc(`${state.recommendedSkills.length} recommended`)}</span></div></div><div class="result-grid"><div class="result-card"><p class="result-meta">Current Skills</p><div class="tag-row">${tagList(data.user_skills)}</div></div><div class="result-card"><p class="result-meta">Missing Skills</p><div class="tag-row">${tagList(data.skill_gaps, "tag-secondary")}</div></div></div></div>`;
}

function renderRecommended() {
    toggleEmpty(els.recommendedSkills, !state.recommendedSkills.length);
    els.recommendedSkills.innerHTML = !state.recommendedSkills.length ? "Recommended skills appear here after skill-gap analysis." : state.recommendedSkills.map(skill => `<article class="skill-card ${state.selectedSkill === skill ? "selected-skill-card" : ""}"><div class="card-header"><div><p class="card-meta">Recommended next skill</p><h4 class="card-headline">${esc(skill)}</h4></div>${state.selectedSkill === skill ? '<span class="tag tag-primary">Selected</span>' : ""}</div><p class="card-body">Validate the right starting level in Agent 1, then hand this skill into Agent 2 for topics, videos, practice, and code.</p><div class="action-row"><button class="button button-primary button-small" data-skill-select="${esc(skill)}">Assess This Skill</button></div></article>`).join("");
}

function renderAssessment() {
    toggleEmpty(els.assessmentQuestions, !state.assessmentQuestions.length);
    toggleEmpty(els.validatedLevelResult, !state.validatedLevel);
    els.assessmentQuestions.innerHTML = !state.assessmentQuestions.length
        ? "Select a recommended skill and generate questions."
        : state.assessmentQuestions.map((q, i) => questionMarkup(q, state.assessmentAnswers[i] || "", i, "assessment")).join("");
    els.validatedLevelResult.innerHTML = !state.validatedLevel
        ? "Validated level, confidence, and reasoning will appear here."
        : `<div class="results-stack"><div class="mini-kv"><div class="mini-kv-item"><span class="summary-label">Declared</span><strong>${esc(state.validatedLevel.declared_level || state.declaredLevel)}</strong></div><div class="mini-kv-item"><span class="summary-label">Validated</span><strong>${esc(state.validatedLevel.validated_level || "Unknown")}</strong></div></div><div class="result-card"><p class="result-meta">Confidence</p><h4 class="result-title">${esc(String(state.validatedLevel.confidence ?? "N/A"))}</h4><p class="result-body">${esc(state.validatedLevel.reasoning || "Validation completed.")}</p></div></div>`;
}

function renderTopics() {
    toggleEmpty(els.topicsList, !state.topics.length);
    els.topicsList.innerHTML = !state.topics.length
        ? "No topics yet."
        : state.topics.map(topic => `<article class="topic-card ${state.selectedTopic === topic ? "selected-skill-card" : ""}"><div class="card-header"><div><p class="card-meta">Recommended topic</p><h4 class="topic-title">${esc(topic)}</h4></div>${state.selectedTopic === topic ? '<span class="tag tag-primary">Current</span>' : ""}</div><p class="card-body">Topics stay inside the selected skill and validated level. Replace or mark them as known without resetting the learning page.</p><div class="action-row"><button class="topic-action" data-topic-select="${esc(topic)}">Select</button><button class="topic-action" data-topic-replace="${esc(topic)}">Replace</button><button class="topic-action" data-topic-known="${esc(topic)}">Known</button></div></article>`).join("");
}

function renderSchedule() {
    els.scheduleControls.classList.toggle("hidden", state.scheduleChoice !== "create");
    document.querySelectorAll("[data-schedule-toggle]").forEach(btn => btn.classList.toggle("mode-pill-active", btn.dataset.scheduleToggle === state.scheduleChoice));
    document.querySelectorAll("[data-schedule-mode]").forEach(btn => btn.classList.toggle("mode-pill-active", btn.dataset.scheduleMode === state.scheduleMode));
    if (!state.schedule) {
        toggleEmpty(els.scheduleView, true);
        els.scheduleView.innerHTML = state.scheduleChoice === "create"
            ? "Choose a schedule mode, daily time, and generate the plan."
            : "No schedule created. Agent 2 can still continue without one.";
        return;
    }
    toggleEmpty(els.scheduleView, false);
    const days = (state.schedule.daily_plan || []).slice(0, 3).map(day => `<div class="result-card"><p class="result-meta">Day ${esc(day.day)}</p><h4 class="result-title">${esc(day.topic || "Learning block")}</h4><p class="result-body">${esc(day.goal || "Structured practice and review.")}</p><div class="tag-row"><span class="tag">${esc(day.difficulty || "Mixed")}</span><span class="tag ${day.completed ? "tag-primary" : ""}">${day.completed ? "Completed" : "Pending"}</span></div></div>`).join("");
    els.scheduleView.innerHTML = `<div class="results-stack"><div class="mini-kv"><div class="mini-kv-item"><span class="summary-label">Mode</span><strong>${esc(fmt(state.schedule.mode))}</strong></div><div class="mini-kv-item"><span class="summary-label">Progress</span><strong>${esc(String(Math.round(Number(state.schedule.progress_percentage || 0))))}%</strong></div><div class="mini-kv-item"><span class="summary-label">Current Day</span><strong>${esc(String(state.schedule.current_day || 1))}</strong></div><div class="mini-kv-item"><span class="summary-label">Daily Time</span><strong>${esc(String(state.schedule.daily_time || els.dailyTimeInput.value || 60))} min</strong></div></div><div class="results-stack">${days || '<div class="result-card"><p class="result-body">Schedule plan will appear here.</p></div>'}</div></div>`;
}

function renderVideos() {
    if (!state.videos.length) {
        toggleEmpty(els.videosList, true);
        els.videosList.innerHTML = "No videos yet.";
        return;
    }
    toggleEmpty(els.videosList, false);
    const curated = state.videos.filter(video => video.source === "curated").length;
    const live = state.videos.filter(video => video.source === "live").length;
    const fallback = state.videos.filter(video => video.source === "fallback").length;
    
    const safeUrl = (urlStr) => {
        if (!urlStr) return "#";
        try {
            const u = new URL(urlStr);
            if (u.protocol === "http:" || u.protocol === "https:") return u.href;
        } catch { /* Ignored */ }
        return "#";
    };

    els.videosList.innerHTML = state.videos.map((video, index) => {
        const title = esc(video.title || "Video");
        const channel = esc(video.channel || "Unknown channel");
        const duration = esc(video.duration || "Unknown");
        const thumbAlt = esc(video.title || "Video thumbnail");
        const url = esc(safeUrl(video.url));
        const thumbUrl = video.thumbnail ? esc(safeUrl(video.thumbnail)) : null;
        const sourceLabel = esc(fmt(video.source || "live"));
        const isSelected = state.selectedVideo?.url === video.url;
        
        const scoreText = (video.learning_fit_score !== undefined && video.learning_fit_score !== null)
            ? `<span class="tag tag-primary">Learning Fit Score: ${esc(String(video.learning_fit_score))}</span>` 
            : "";
            
        const fallbackText = video.is_semantic_fallback 
            ? `<span class="tag tag-secondary">Semantic evaluation unavailable</span>` 
            : "";

        const showSemanticFields = !video.is_semantic_fallback;

        const shortDescHtml = video.short_description
            ? `<p class="card-meta">${esc(video.short_description)}</p>`
            : "";

        const descHtml = video.description 
            ? `<p class="card-body">${esc(video.description)}</p>` 
            : "";

        const conceptsHtml = (showSemanticFields && Array.isArray(video.concepts_covered) && video.concepts_covered.length > 0)
            ? `<div class="tag-row">${tagList(video.concepts_covered)}</div>`
            : "";

        const whyHtml = (showSemanticFields && Array.isArray(video.why_recommended) && video.why_recommended.length > 0)
            ? `<ul class="job-description">${video.why_recommended.map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>`
            : "";

        return `<article class="video-card ${isSelected ? "selected-skill-card" : ""}">
            <div class="video-thumb">
                ${thumbUrl !== null && thumbUrl !== "#" ? `<img src="${thumbUrl}" alt="${thumbAlt}">` : `<div class="video-thumb-placeholder"><span class="material-symbols-outlined">smart_display</span><strong>${duration}</strong></div>`}
                <span class="source-badge ${video.source === "curated" ? "source-curated" : "source-live"}">${sourceLabel}</span>
                <span class="duration-badge">${duration}</span>
            </div>
            <div class="video-content">
                <p class="video-title">${title}</p>
                <p class="card-meta">${channel}</p>
                ${(scoreText || fallbackText) ? `<div class="tag-row">${scoreText}${fallbackText}</div>` : ""}
                ${shortDescHtml}
                ${conceptsHtml}
                ${whyHtml}
                ${descHtml}
                <div class="video-actions">
                    <button class="button button-secondary button-small" data-video-select="${index}">${isSelected ? "Selected for Practice" : "Use for Practice"}</button>
                    <a class="button button-primary button-small" href="${url}" target="_blank" rel="noopener noreferrer">Watch on YouTube</a>
                </div>
            </div>
        </article>`;
    }).join("");
    
    if (fallback) {
        setStatus(els.videosMeta, `Direct YouTube results were unavailable. Showing ${fallback} backup YouTube searches for this topic.`, "warning");
    } else {
        setStatus(els.videosMeta, `Returned ${state.videos.length} videos (${curated} curated, ${live} live).`, "success");
    }
}

function renderPractice() {
    if (els.evaluatePracticeBtn) {
        const hasPracticeQuestions = Boolean(state.practicePack?.questions?.length);
        els.evaluatePracticeBtn.textContent = "Submit Practice Answers";
        els.evaluatePracticeBtn.disabled = !hasPracticeQuestions;
    }
    if (!state.practicePack) {
        toggleEmpty(els.practiceQuestions, true);
        toggleEmpty(els.miniLab, true);
        els.practiceQuestions.innerHTML = "No practice pack yet.";
        els.miniLab.innerHTML = "Mini-lab details will appear here.";
        return;
    }
    toggleEmpty(els.practiceQuestions, false);
    toggleEmpty(els.miniLab, !state.practicePack.mini_lab);
    els.practiceQuestions.innerHTML = (state.practicePack.questions || []).map((q, i) => questionMarkup(q, state.practiceAnswers[i] || "", i, "practice")).join("");
    setStatus(els.practiceSummary, state.practicePack.practice_summary || "Practice pack generated successfully.", "success");
    const lab = state.practicePack.mini_lab;
    els.miniLab.innerHTML = !lab
        ? "Mini-lab details are unavailable."
        : `<div class="mini-lab-card"><div class="card-header"><div><p class="card-meta">Mini-lab</p><h4 class="card-headline">${esc(lab.title || "Mini-lab")}</h4></div><span class="tag tag-primary">${esc(lab.difficulty || "Guided")}</span></div><p class="card-body">${esc(lab.prompt || "No prompt returned.")}</p><div class="mini-kv"><div class="mini-kv-item"><span class="summary-label">Estimated Time</span><strong>${esc(String(lab.estimated_minutes || "N/A"))} min</strong></div><div class="mini-kv-item"><span class="summary-label">Context</span><strong>${esc(lab.real_world_context || "Contextual practice")}</strong></div></div>${(lab.test_cases || []).length ? `<div class="result-card"><p class="result-meta">Test Cases</p><ul class="job-description">${lab.test_cases.map(item => `<li>${esc(typeof item === "string" ? item : JSON.stringify(item))}</li>`).join("")}</ul></div>` : ""}${(lab.hints || []).length ? `<div class="result-card"><p class="result-meta">Hint Prompts</p><ul class="job-description">${lab.hints.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}${lab.starter_code ? `<div class="action-row"><button class="button button-secondary button-small" data-load-starter="true">Load Mini-lab Starter</button></div>` : ""}</div>`;
}

function renderPracticeEvaluation() {
    toggleEmpty(els.practiceEvaluation, !state.practiceEvaluation);
    els.practiceEvaluation.innerHTML = !state.practiceEvaluation
        ? "Practice evaluation will appear here."
        : `<div class="results-stack"><div class="mini-kv"><div class="mini-kv-item"><span class="summary-label">Accepted</span><strong>${esc(String(state.practiceEvaluation.accepted_count || 0))}/${esc(String(state.practiceEvaluation.total_questions || 0))}</strong></div><div class="mini-kv-item"><span class="summary-label">Next Step</span><strong>${esc(state.practiceEvaluation.recommended_next_step || "Continue")}</strong></div></div>${(state.practiceEvaluation.items || []).map(item => `<div class="result-card"><p class="result-meta">${item.acceptable ? "Accepted" : "Needs work"}</p><h4 class="result-title">${esc(item.question || "Question")}</h4><p class="result-body">${esc(item.feedback || "Feedback unavailable.")}</p></div>`).join("")}<div class="result-card"><p class="result-meta">Overall Feedback</p><p class="result-body">${esc(state.practiceEvaluation.overall_feedback || "Practice evaluation completed.")}</p></div></div>`;
}

function renderHint() {
    toggleEmpty(els.hintOutput, !state.hint);
    els.hintOutput.innerHTML = !state.hint
        ? "Hints will appear here and preserve the current code context."
        : `<div class="result-card"><p class="result-meta">Hint level ${esc(String(state.hint.hint_level || state.hintLevel))}</p><h4 class="result-title">${esc(state.hint.next_focus || "Next focus")}</h4><p class="result-body">${esc(state.hint.hint || "No hint returned.")}</p>${state.hint.question ? `<p class="card-meta">Reflection prompt: ${esc(state.hint.question)}</p>` : ""}</div>`;
}

function renderCode() {
    els.languageSelect.innerHTML = !state.executionConfig?.languages?.length ? `<option value="">No languages loaded</option>` : state.executionConfig.languages.map(lang => `<option value="${esc(lang.id)}">${esc(lang.label)}</option>`).join("");
    if (state.currentLanguage) els.languageSelect.value = state.currentLanguage;
    if (els.runtimeNote) {
        const notes = Array.isArray(state.executionConfig?.runtime_notes) ? state.executionConfig.runtime_notes : [];
        const pythonPackages = Array.isArray(state.executionConfig?.preinstalled_python_packages) ? state.executionConfig.preinstalled_python_packages : [];
        if (notes.length) {
            const packageText = pythonPackages.length ? ` Available Python libs: ${pythonPackages.join(", ")}.` : "";
            els.runtimeNote.textContent = `${notes.join(" ")}${packageText}`;
            els.runtimeNote.dataset.state = "default";
        } else {
            els.runtimeNote.textContent = "Runtime capabilities will appear here after the execution config loads.";
            els.runtimeNote.dataset.state = "warning";
        }
    }
    els.codeTextarea.value = state.code || "";
    els.validationOutput.textContent = state.validationOutput;
    els.executionOutput.textContent = state.executionOutput;
}

function renderEvaluation() {
    const hasQuestions = Boolean(state.evaluationPack?.questions?.length);
    if (els.generateEvaluationBtn) {
        els.generateEvaluationBtn.textContent = hasQuestions ? "Questions Ready" : "Generate Questions";
        els.generateEvaluationBtn.disabled = hasQuestions;
    }
    if (els.scoreEvaluationBtn) {
        els.scoreEvaluationBtn.textContent = "Submit Final Evaluation";
        els.scoreEvaluationBtn.disabled = !hasQuestions;
    }
    if (els.evaluationMeta) {
        if (state.evaluationResult) {
            setStatus(els.evaluationMeta, "Evaluation complete. Readiness result is saved and visible below.", "success");
        } else if (hasQuestions) {
            setStatus(els.evaluationMeta, "Evaluation questions are ready below. Answer them, then use Submit Final Evaluation.", "success");
        } else if (!els.evaluationMeta.dataset.state || els.evaluationMeta.dataset.state === "success") {
            setStatus(els.evaluationMeta, "Generate a question pack once, answer everything, then use Submit Final Evaluation to produce the readiness result.", "default");
        }
    }
    toggleEmpty(els.evaluationPack, !hasQuestions);
    els.evaluationPack.innerHTML = !hasQuestions ? "No evaluation pack generated yet." : state.evaluationPack.questions.map((q, i) => questionMarkup(q, state.evaluationAnswers[i] || "", i, "evaluation")).join("");
    if (!state.evaluationResult) {
        toggleEmpty(els.evaluationResult, true);
        els.evaluationResult.innerHTML = "Your scored readiness result will appear here.";
        return;
    }
    toggleEmpty(els.evaluationResult, false);
    const result = state.evaluationResult;
    
    const roleHtml = state.targetRole ? `<div class="mini-kv-item"><span class="summary-label">Target Role</span><strong>${esc(state.targetRole)}</strong></div>` : "";
    const summaryHtml = `<div class="mini-kv"><div class="mini-kv-item"><span class="summary-label">Skill</span><strong>${esc(state.selectedSkill || "N/A")}</strong></div><div class="mini-kv-item"><span class="summary-label">Topic</span><strong>${esc(state.selectedTopic || "N/A")}</strong></div><div class="mini-kv-item"><span class="summary-label">Level</span><strong>${esc(learnLevel() || "N/A")}</strong></div>${roleHtml}</div>`;

    const hasProgress = state.scheduleChoice === "create" && state.schedule && typeof state.schedule.progress_percentage !== 'undefined';
    const progressVal = hasProgress ? Math.min(100, Math.max(0, parseInt(state.schedule.progress_percentage))) : 0;
    const progressText = hasProgress ? `${progressVal}%` : "No plan";
    const progressStroke = hasProgress ? "var(--primary)" : "var(--border)";
    const circumference = 2 * Math.PI * 36; 
    const strokeDashoffset = circumference - (progressVal / 100) * circumference;
    
    const ringHtml = `
    <div class="progress-ring-container" style="text-align:center; display:flex; flex-direction:column; align-items:center;">
        <svg width="100" height="100" viewBox="0 0 80 80">
            <circle cx="40" cy="40" r="36" fill="none" stroke="var(--bg-panel)" stroke-width="6"/>
            <circle cx="40" cy="40" r="36" fill="none" stroke="${progressStroke}" stroke-width="6" stroke-dasharray="${circumference}" stroke-dashoffset="${hasProgress ? strokeDashoffset : 0}" style="transform: rotate(-90deg); transform-origin: 50% 50%;"/>
        </svg>
        <div style="margin-top:-60px; margin-bottom: 30px; font-weight:bold; font-size: 1.1rem; color: var(--text-primary);">${esc(progressText)}</div>
        <p class="summary-label" style="margin-top:10px;">${hasProgress ? `Day ${esc(state.schedule.current_day || 0)} of ${esc(state.schedule.total_days || 0)}` : "No learning plan yet"}</p>
    </div>`;

    let planHtml = "";
    if (state.scheduleChoice === "create" && state.schedule && state.schedule.daily_plan) {
        const tasks = Array.isArray(state.schedule.daily_plan) ? state.schedule.daily_plan : [];
        planHtml = `<div class="result-card" style="grid-column: 1 / -1;"><p class="result-meta">Learning Plan</p><ul class="job-description">${tasks.map(t => `<li>${esc(t.task || t.description || t)}</li>`).join("")}</ul></div>`;
    }

    els.evaluationResult.innerHTML = `
        <div class="results-stack">
            ${summaryHtml}
            <div class="dashboard-metrics-grid" style="display:flex; flex-wrap:wrap; gap:1rem; align-items:stretch; margin-top:1rem; margin-bottom:1rem;">
                <div class="result-card" style="flex:1; min-width:200px;">
                    <p class="result-meta">Learning Progress</p>
                    ${ringHtml}
                </div>
                <div class="result-card" style="flex:2; min-width:250px; display:flex; flex-direction:column; justify-content:center;">
                    <p class="result-meta" style="margin-bottom:1rem;">Assessment Metrics</p>
                    <div class="mini-kv">
                        <div class="mini-kv-item"><span class="summary-label">Score</span><strong>${esc(String(result.total_score ?? "N/A"))}</strong></div>
                        <div class="mini-kv-item"><span class="summary-label">Mastery</span><strong>${esc(result.badge || "Pending")}</strong></div>
                        <div class="mini-kv-item"><span class="summary-label">Readiness</span><strong>${esc(result.readiness || "Pending")}</strong></div>
                        ${result.confidence ? `<div class="mini-kv-item"><span class="summary-label">Confidence</span><strong>${esc(String(result.confidence))}</strong></div>` : ""}
                    </div>
                </div>
            </div>
            <div class="result-grid">
                <div class="result-card"><p class="result-meta">Strengths</p><div class="tag-row">${tagList(result.strengths, "tag-primary")}</div></div>
                <div class="result-card"><p class="result-meta">Weak Topics</p><div class="tag-row">${tagList(result.weak_topics, "tag-secondary")}</div></div>
                <div class="result-card" style="grid-column: 1 / -1;"><p class="result-meta">Next Steps</p><div class="tag-row">${tagList(result.next_steps)}</div></div>
                ${result.feedback ? `<div class="result-card" style="grid-column: 1 / -1;"><p class="result-meta">Feedback</p><p class="result-body">${esc(result.feedback)}</p></div>` : ""}
                ${planHtml}
            </div>
        </div>
    `;
}

function renderJobs() {
    toggleEmpty(els.jobsList, !state.jobs.length);
    els.jobsList.innerHTML = !state.jobs.length
        ? "Job recommendations appear here after evaluation or manual fetch."
        : state.jobs.map(job => `<article class="job-card"><div class="card-header"><div><p class="job-meta">${esc(job.company || "Unknown company")}</p><h4 class="job-title">${esc(job.title || "Job title")}</h4></div><span class="fit-badge ${job.fit_level === "stretch" ? "fit-stretch" : "fit-strong"}">${esc(fmt(job.fit_level || "fit"))}</span></div><p class="job-meta">${esc(job.location || "Location not provided")}${job.employment_type ? ` - ${esc(job.employment_type)}` : ""}</p><p class="job-description">${esc(job.description || "No description returned.")}</p><div class="job-actions">${job.apply_url ? `<a class="button button-primary button-small" href="${esc(job.apply_url)}" target="_blank" rel="noopener noreferrer">Apply</a>` : ""}</div></article>`).join("");
}

function render() {
    els.targetRoleInput.value = state.targetRole || "";
    if (els.manualSkillInput) els.manualSkillInput.value = state.selectedSkill || "";
    renderResume();
    renderGap();
    renderTrending();
    renderRecommended();
    renderAssessment();
    renderTopics();
    renderSchedule();
    renderVideos();
    renderPractice();
    renderPracticeEvaluation();
    renderHint();
    renderCode();
    renderEvaluation();
    renderJobs();
    updateSummary();
}

async function ensureSession(force = false) {
    if (force) {
        stopGapPoll();
        const retainedConfig = state.executionConfig;
        const retainedLanguage = state.currentLanguage;
        const retainedStarter = state.starterCode;
        const retainedCode = state.code;
        const retainedRole = state.targetRole;
        const retainedDuration = state.preferredDuration;
        Object.assign(state, { ...defaults });
        state.executionConfig = retainedConfig;
        state.currentLanguage = retainedLanguage;
        state.starterCode = retainedStarter;
        state.code = retainedCode;
        state.targetRole = retainedRole || defaults.targetRole;
        state.preferredDuration = retainedDuration || defaults.preferredDuration;
        setInitialSelections();
        render();
    }
    if (state.sessionId && !force) return state.sessionId;
    const data = await api("/api/session/start", { method: "POST" });
    state.sessionId = data.session_id;
    updateSummary();
    toast("Session ready", `Session ${state.sessionId} created.`, "success");
    return state.sessionId;
}

function seedStarter(force = false) {
    const langs = state.executionConfig?.languages || [];
    if (!langs.length) return;
    const current = langs.find(lang => lang.id === state.currentLanguage) || langs[0];
    state.currentLanguage = current.id;
    const starter = current.starter_code || "";
    if (force || !state.code || state.code === defaults.code || state.code === state.starterCode) state.code = starter;
    state.starterCode = starter;
}

async function loadExecutionConfig() {
    try {
        state.executionConfig = await api("/api/execution-config");
        if (!state.currentLanguage) state.currentLanguage = state.executionConfig.default_language || state.executionConfig.languages?.[0]?.id || null;
        seedStarter();
    } catch (error) {
        state.validationOutput = `Failed to load execution config.\n${error.message}`;
        toast("Executor config", error.message, "error");
    }
    render();
}

async function handleStartSession() {
    await task("start-session", els.startSessionBtn, async () => {
        try {
            await ensureSession(Boolean(state.sessionId));
            setActiveStage("discover-stage");
            setStatus(els.discoverStatus, "Session ready. Resume analysis and skill-gap discovery can start now.", "success");
        } catch (error) {
            setStatus(els.discoverStatus, error.message, "error");
            toast("Session error", error.message, "error");
        }
    });
}

async function handleAnalyzeResume() {
    await task("resume", els.analyzeResumeBtn, async () => {
        if (!els.resumeFile.files[0]) {
            setStatus(els.discoverStatus, "Choose a resume file before analyzing.", "warning");
            return;
        }
        try {
            await ensureSession();
            setStatus(els.discoverStatus, "Uploading resume and analyzing profile...", "loading");
            const form = new FormData();
            form.append("resume", els.resumeFile.files[0]);
            form.append("session_id", state.sessionId);
            state.resumeSummary = await api("/api/analyze-resume", { method: "POST", body: form });
            setStatus(els.discoverStatus, "Resume analysis complete.", "success");
            render();
        } catch (error) {
            setStatus(els.discoverStatus, error.message, "error");
            toast("Resume analysis failed", error.message, "error");
        }
    });
}

async function handleTrending() {
    await task("trending", els.fetchTrendingBtn, async () => {
        try {
            state.targetRole = els.targetRoleInput.value.trim() || defaults.targetRole;
            els.trendingMeta.textContent = `Live market demand for ${state.targetRole}`;
            setStatus(els.discoverStatus, "Fetching trending skills...", "loading");
            const data = await api("/api/trending-skills", { method: "POST", body: { target_role: state.targetRole } });
            state.trendingSkills = Array.isArray(data.skills) ? data.skills : [];
            setStatus(els.discoverStatus, `Fetched ${state.trendingSkills.length} trending skills for ${state.targetRole}.`, "success");
            render();
        } catch (error) {
            setStatus(els.discoverStatus, error.message, "error");
            toast("Trending skills failed", error.message, "error");
        }
    });
}

function stopGapPoll() {
    if (!gapTimer) return;
    clearTimeout(gapTimer);
    gapTimer = null;
}

async function pollGap(jobId, attempt = 0) {
    stopGapPoll();
    if (attempt > GAP_MAX_POLLS) {
        setStatus(els.skillGapStatus, "Polling timed out. Retry skill-gap analysis when ready.", "warning");
        return;
    }
    try {
        const data = await api(`/api/skill-gaps/${encodeURIComponent(jobId)}`);
        const status = String(data.status || "").toLowerCase();
        if (status === "failed" || status === "error") throw new Error(data.error || data.message || "Skill-gap job failed.");
        if (status === "completed" || status === "success" || data.result) {
            state.skillGapResult = data;
            setStatus(els.skillGapStatus, "Skill-gap analysis complete. Recommended skills are ready.", "success");
            render();
            return;
        }
        setStatus(els.skillGapStatus, data.phase || data.message || gapPhases[Math.min(attempt, gapPhases.length - 1)], "loading");
        gapTimer = setTimeout(() => pollGap(jobId, attempt + 1), GAP_INTERVAL);
    } catch (error) {
        setStatus(els.skillGapStatus, error.message, "error");
        toast("Skill-gap polling failed", error.message, "error");
    }
}

async function handleSkillGap() {
    await task("skill-gap", els.startSkillGapBtn, async () => {
        try {
            await ensureSession();
            state.targetRole = els.targetRoleInput.value.trim() || defaults.targetRole;
            const body = { session_id: state.sessionId, target_role: state.targetRole };
            if (Array.isArray(state.resumeSummary?.user_skills) && state.resumeSummary.user_skills.length) body.user_skills = state.resumeSummary.user_skills;
            setStatus(els.skillGapStatus, "Starting async skill-gap analysis...", "loading");
            const data = await api("/api/skill-gaps/start", { method: "POST", body });
            state.skillGapJobId = data.job_id;
            state.skillGapResult = null;
            render();
            pollGap(data.job_id, 0);
        } catch (error) {
            setStatus(els.skillGapStatus, error.message, "error");
            toast("Skill-gap failed", error.message, "error");
        }
    });
}

function applySkill(skill) {
    const normalized = String(skill || "").trim();
    if (!normalized) return;
    state.selectedSkill = normalized;
    if (els.manualSkillInput) els.manualSkillInput.value = normalized;
    state.assessmentQuestions = [];
    state.assessmentAnswers = [];
    state.validatedLevel = null;
    state.selectedTopic = null;
    state.topics = [];
    state.knownTopics = [];
    state.excludedTopics = [];
    state.videos = [];
    state.selectedVideo = null;
    state.schedule = null;
    state.scheduleChoice = "none";
    state.practicePack = null;
    state.practiceAnswers = [];
    state.practiceEvaluation = null;
    state.hint = null;
    state.evaluationPack = null;
    state.evaluationAnswers = [];
    state.evaluationResult = null;
    state.jobs = [];
    render();
    setStatus(els.discoverStatus, `${normalized} selected. Choose a declared level and validate it before entering Agent 2.`, "success");
    setStatus(els.topicsStatus, "Generate a fresh level validation, then fetch topics for the selected skill.", "warning");
}

function handleManualSkill() {
    const value = els.manualSkillInput?.value?.trim();
    if (!value) {
        setStatus(els.discoverStatus, "Enter a skill name before using manual skill entry.", "warning");
        return;
    }
    applySkill(value);
}

async function handleAssessment() {
    await task("assessment", els.generateAssessmentBtn, async () => {
        if (!state.selectedSkill) {
            setStatus(els.discoverStatus, "Select a recommended skill before generating assessment questions.", "warning");
            return;
        }
        try {
            await ensureSession();
            setStatus(els.discoverStatus, `Generating validation questions for ${state.selectedSkill}...`, "loading");
            const data = await api("/api/assess-level", { method: "POST", body: { session_id: state.sessionId, skill: state.selectedSkill, declared_level: state.declaredLevel } });
            state.assessmentQuestions = Array.isArray(data.questions) ? data.questions : [];
            state.assessmentAnswers = new Array(state.assessmentQuestions.length).fill("");
            setStatus(els.discoverStatus, "Assessment questions generated. Answer them and validate the level.", "success");
            render();
        } catch (error) {
            setStatus(els.discoverStatus, error.message, "error");
            toast("Assessment generation failed", error.message, "error");
        }
    });
}

async function handleLevelValidation() {
    await task("level-validate", els.validateLevelBtn, async () => {
        if (!state.assessmentQuestions.length) {
            setStatus(els.discoverStatus, "Generate assessment questions first.", "warning");
            return;
        }
        if (state.assessmentAnswers.some(answer => !String(answer || "").trim())) {
            setStatus(els.discoverStatus, "Answer all assessment questions before validating.", "warning");
            return;
        }
        try {
            setStatus(els.discoverStatus, "Validating declared level...", "loading");
            state.validatedLevel = await api("/api/validate-level", { method: "POST", body: { session_id: state.sessionId, answers: state.assessmentAnswers, questions: state.assessmentQuestions, declared_level: state.declaredLevel } });
            setStatus(els.discoverStatus, `Validated level: ${state.validatedLevel.validated_level}. Agent 2 is ready.`, "success");
            render();
            setActiveStage("learn-stage");
        } catch (error) {
            setStatus(els.discoverStatus, error.message, "error");
            toast("Level validation failed", error.message, "error");
        }
    });
}

async function handleTopics(extra = []) {
    await task("topics", els.fetchTopicsBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before requesting Agent 2 topics.")) {
            setStatus(els.topicsStatus, "Select and validate a skill in Agent 1 first.", "warning");
            return;
        }
        try {
            setStatus(els.topicsStatus, "Generating recommended topics...", "loading");
            const exclude = [...new Set([...state.knownTopics, ...state.excludedTopics, ...extra])];
            const data = await api("/api/topics", { method: "POST", body: { skill: state.selectedSkill, level: learnLevel(), count: 3, exclude_topics: exclude } });
            state.topics = Array.isArray(data.topics) ? data.topics : [];
            state.excludedTopics = exclude;
            state.selectedTopic = state.topics.includes(state.selectedTopic) ? state.selectedTopic : (state.topics[0] || null);
            setStatus(els.topicsStatus, `Generated ${state.topics.length} topic recommendations.`, "success");
            render();
        } catch (error) {
            setStatus(els.topicsStatus, error.message, "error");
            toast("Topic recommendation failed", error.message, "error");
        }
    });
}

async function handleVideos() {
    await task("videos", els.fetchVideosBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before fetching videos.")) {
            setStatus(els.videosMeta, "Validate the skill level in Agent 1 first.", "warning");
            return;
        }
        if (!state.selectedSkill || !state.selectedTopic) {
            setStatus(els.videosMeta, "Choose a skill and topic before fetching videos.", "warning");
            return;
        }
        try {
            setStatus(els.videosMeta, "Fetching curated and live YouTube recommendations...", "loading");
            const data = await api("/api/videos", { method: "POST", body: { skill: state.selectedSkill, level: learnLevel(), topic: state.selectedTopic, preferred_duration: state.preferredDuration, max_results: 12 } });
            state.videos = Array.isArray(data.videos) ? data.videos : [];
            render();
            els.videosPanel?.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
            setStatus(els.videosMeta, error.message, "error");
            toast("Video fetch failed", error.message, "error");
        }
    });
}

async function handlePractice() {
    await task("practice", els.generatePracticeBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before generating practice.")) {
            setStatus(els.practiceSummary, "Validate the skill level in Agent 1 first.", "warning");
            return;
        }
        if (!state.selectedSkill || !state.selectedTopic) {
            setStatus(els.practiceSummary, "Choose a skill and topic before generating practice.", "warning");
            return;
        }
        try {
            await ensureSession();
            setStatus(els.practiceSummary, "Generating level-aware practice and mini-lab...", "loading");
            const body = { session_id: state.sessionId, skill: state.selectedSkill, topic: state.selectedTopic, level: learnLevel(), language: state.currentLanguage || "python" };
            if (state.selectedVideo) body.video_context = { title: state.selectedVideo.title, channel: state.selectedVideo.channel, duration: state.selectedVideo.duration, source: state.selectedVideo.source, url: state.selectedVideo.url };
            state.practicePack = await api("/api/practice", { method: "POST", body });
            state.practiceAnswers = new Array(state.practicePack.questions?.length || 0).fill("");
            state.practiceEvaluation = null;
            if (state.practicePack.mini_lab?.starter_code && (!state.code || state.code === state.starterCode)) {
                state.code = state.practicePack.mini_lab.starter_code;
                state.starterCode = state.practicePack.mini_lab.starter_code;
            }
            render();
            els.practicePanel?.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
            setStatus(els.practiceSummary, error.message, "error");
            toast("Practice generation failed", error.message, "error");
        }
    });
}

async function handlePracticeEvaluation() {
    await task("practice-eval", els.evaluatePracticeBtn, async () => {
        if (!state.practicePack?.questions?.length) {
            setStatus(els.practiceSummary, "Generate a practice pack first.", "warning");
            return;
        }
        if (state.practiceAnswers.some(answer => !String(answer || "").trim())) {
            setStatus(els.practiceSummary, "Answer all practice questions before evaluation.", "warning");
            return;
        }
        try {
            setStatus(els.practiceSummary, "Evaluating practice answers...", "loading");
            state.practiceEvaluation = await api("/api/practice/evaluate", { method: "POST", body: { session_id: state.sessionId, skill: state.selectedSkill, topic: state.selectedTopic, answers: state.practiceAnswers } });
            setStatus(els.practiceSummary, "Practice evaluation complete.", "success");
            render();
        } catch (error) {
            setStatus(els.practiceSummary, error.message, "error");
            toast("Practice evaluation failed", error.message, "error");
        }
    });
}

async function handleHint() {
    await task("hint", els.requestHintBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before requesting hints.")) {
            return;
        }
        if (!state.selectedSkill || !state.selectedTopic) {
            toast("Hint unavailable", "Pick a skill and topic before requesting a hint.", "error");
            return;
        }
        try {
            state.hint = await api("/api/hint", { method: "POST", body: { session_id: state.sessionId, skill: state.selectedSkill, topic: state.selectedTopic, level: learnLevel(), code: state.code, error: state.executionOutput, hint_level: state.hintLevel } });
            render();
            toast("Hint ready", "A new hint has been added to the support panel.", "success");
        } catch (error) {
            toast("Hint failed", error.message, "error");
        }
    });
}

async function handleValidateCode() {
    await task("code-validate", els.validateCodeBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before using the coding workspace.")) {
            return;
        }
        if (!state.code.trim()) {
            state.validationOutput = "Add code before validating.";
            renderCode();
            return;
        }
        try {
            state.validationOutput = "Validating code...";
            renderCode();
            const data = await api("/api/validate-code", { method: "POST", body: { code: state.code, language: state.currentLanguage } });
            state.validationOutput = data.valid ? `Valid ${data.language} code.` : `Issues found:\n${(data.errors || []).map(e => e.line != null ? \`Line ${e.line}: ${e.message}\` : e.message).join("\n")}`;
            renderCode();
        } catch (error) {
            state.validationOutput = `Validation failed.\n${error.message}`;
            renderCode();
            toast("Validation failed", error.message, "error");
        }
    });
}

async function handleRunCode() {
    await task("code-run", els.runCodeBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before running code.")) {
            return;
        }
        if (!state.code.trim()) {
            state.executionOutput = "Add code before running.";
            renderCode();
            return;
        }
        try {
            state.executionOutput = "Running code...";
            renderCode();
            
            const taskContext = {
                skill: state.selectedSkill,
                topic: state.selectedTopic,
                level: learnLevel(),
                practice_task: state.practicePack?.mini_lab?.task || "No specific task assigned"
            };
            
            const payload = { 
                code: state.code, 
                language: state.currentLanguage, 
                stdin: els.stdinInput.value || "",
                context: taskContext
            };
            
            const data = await api("/api/execute-code", { method: "POST", body: payload });
            
            if (data.fallback && data.validation) {
                const val = data.validation;
                const lines = [`AI Code Validation: ${val.status}`];
                if (val.status === "INVALID" && val.errors?.length) {
                    val.errors.forEach(err => {
                        const lineStr = err.line ? ` (Line ${err.line})` : "";
                        lines.push(`- Error${lineStr}: ${err.message}`);
                    });
                }
                if (val.explanation) lines.push("", `Explanation:\n${val.explanation}`);
                if (val.suggestion) lines.push("", `Suggestion:\n${val.suggestion}`);
                state.executionOutput = lines.join("\n");
            } else {
                const lines = [`Success: ${data.success}`, `Exit code: ${data.exit_code ?? "N/A"}`];
                if (data.stdout) lines.push("", "STDOUT:", data.stdout);
                if (data.stderr) lines.push("", "STDERR:", data.stderr);
                if (data.error) lines.push("", "ERROR:", data.error);
                state.executionOutput = lines.join("\n");
            }
            renderCode();
        } catch (error) {
            state.executionOutput = `Execution failed.\n${error.message}`;
            renderCode();
            toast("Execution failed", error.message, "error");
        }
    });
}

async function handleSchedule() {
    await task("schedule", els.generateScheduleBtn, async () => {
        if (state.scheduleChoice !== "create") {
            els.scheduleView.innerHTML = "Schedule creation is off. Switch to Create Schedule to generate one.";
            return;
        }
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before generating a schedule.")) {
            toast("Schedule unavailable", "Select and validate a skill before generating a schedule.", "error");
            return;
        }
        try {
            await ensureSession();
            state.schedule = await api("/api/schedule", { method: "POST", body: { session_id: state.sessionId, skill: state.selectedSkill, level: learnLevel(), mode: state.scheduleMode, daily_time: Number(els.dailyTimeInput.value || 60) } });
            state.scheduleChoice = "create";
            render();
            toast("Schedule ready", "The learning plan was generated successfully.", "success");
        } catch (error) {
            els.scheduleView.innerHTML = `Failed to generate schedule. ${esc(error.message)}`;
            toast("Schedule failed", error.message, "error");
        }
    });
}

async function handleLoadSchedule() {
    await task("load-schedule", els.loadScheduleBtn, async () => {
        if (!state.sessionId) {
            toast("No session", "Start a session before loading a saved schedule.", "error");
            return;
        }
        try {
            state.schedule = await api(`/api/schedule/${encodeURIComponent(state.sessionId)}`);
            state.scheduleChoice = "create";
            render();
            setActiveStage("learn-stage");
            toast("Schedule restored", "Saved schedule loaded from the backend.", "success");
        } catch (error) {
            toast("Schedule load failed", error.message, "error");
        }
    });
}

async function handleCompleteDay() {
    await task("complete-day", els.completeDayBtn, async () => {
        if (!state.schedule || !state.sessionId) {
            toast("No schedule", "Generate or load a schedule before updating progress.", "error");
            return;
        }
        try {
            await api("/api/schedule/progress", { method: "POST", body: { session_id: state.sessionId, day: state.schedule.current_day || 1, completed: true } });
            await handleLoadSchedule();
            toast("Progress updated", "Current day marked complete.", "success");
        } catch (error) {
            toast("Progress update failed", error.message, "error");
        }
    });
}

async function handleEvaluation() {
    await task("evaluation-generate", els.generateEvaluationBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before starting Agent 3 evaluation.")) {
            setStatus(els.evaluationMeta, "Select and validate a skill before Agent 3 evaluation.", "warning");
            return;
        }
        if (state.evaluationPack?.questions?.length) {
            setStatus(els.evaluationMeta, "Evaluation questions are already ready below. Answer them and use Submit Final Evaluation.", "success");
            els.evaluationPack?.scrollIntoView({ behavior: "smooth", block: "start" });
            return;
        }
        try {
            await ensureSession();
            state.evaluationPack = null;
            state.evaluationAnswers = [];
            state.evaluationResult = null;
            render();
            setStatus(els.evaluationMeta, "Generating readiness evaluation pack...", "loading");
            state.evaluationPack = await api("/api/evaluate", { method: "POST", body: { session_id: state.sessionId, skill: state.selectedSkill, level: learnLevel(), question_count: 5 } });
            state.evaluationAnswers = new Array(state.evaluationPack.questions?.length || 0).fill("");
            state.evaluationResult = null;
            setStatus(els.evaluationMeta, "Evaluation questions generated. Use Submit Final Evaluation when the responses are ready.", "success");
            render();
            els.evaluationPack?.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
            state.evaluationPack = null;
            state.evaluationAnswers = [];
            state.evaluationResult = null;
            render();
            const message = friendlyError(error);
            setStatus(els.evaluationMeta, `Evaluation generation failed: ${message} Try Generate Questions again.`, "error");
            toast("Evaluation generation failed", message, "error");
        }
    });
}

async function handleEvaluationScore() {
    await task("evaluation-score", els.scoreEvaluationBtn, async () => {
        if (!state.evaluationPack?.questions?.length) {
            setStatus(els.evaluationMeta, "Generate the evaluation pack first.", "warning");
            return;
        }
        if (state.evaluationAnswers.some(answer => !String(answer || "").trim())) {
            setStatus(els.evaluationMeta, "Answer all evaluation questions before final evaluation.", "warning");
            return;
        }
        try {
            setStatus(els.evaluationMeta, "Evaluating readiness answers...", "loading");
            state.evaluationResult = await api("/api/evaluate", { method: "POST", body: { session_id: state.sessionId, skill: state.selectedSkill, level: learnLevel(), questions: state.evaluationPack.questions, answers: state.evaluationAnswers, practice_summary: state.practiceEvaluation || state.practicePack?.practice_summary || null } });
            setStatus(els.evaluationMeta, "Evaluation complete. Readiness result is now saved and visible.", "success");
            render();
            els.evaluationResult?.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
            const message = friendlyError(error);
            setStatus(els.evaluationMeta, `Evaluation did not finish: ${message} Your answers are still preserved below. Tap Submit Final Evaluation once more.`, "warning");
            toast("Evaluation scoring failed", message, "error");
        }
    });
}

async function handleJobs() {
    await task("jobs", els.fetchJobsBtn, async () => {
        if (!requireValidatedSkill("Validate the selected skill level in Agent 1 before fetching jobs.")) {
            toast("Jobs unavailable", "Select and validate a skill before requesting job matches.", "error");
            return;
        }
        try {
            const data = await api("/api/jobs", { method: "POST", body: { skill: state.selectedSkill, level: learnLevel(), limit: 6, session_id: state.sessionId } });
            state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
            renderJobs();
            save();
        } catch (error) {
            toast("Job fetch failed", error.message, "error");
        }
    });
}

async function handleRestoreResult() {
    await task("restore-result", els.restoreResultBtn, async () => {
        if (!state.sessionId) {
            toast("No session", "Start a session before restoring saved results.", "error");
            return;
        }
        try {
            state.evaluationResult = await api(`/api/results/${encodeURIComponent(state.sessionId)}`);
            render();
            setActiveStage("prove-stage");
            toast("Result restored", "Saved readiness result loaded successfully.", "success");
        } catch (error) {
            toast("Restore failed", error.message, "error");
        }
    });
}

function bind() {
    els.stageLinks.forEach(link => link.addEventListener("click", event => {
        event.preventDefault();
        setActiveStage(link.dataset.stageLink);
    }));
    els.startSessionBtn.addEventListener("click", handleStartSession);
    els.restoreResultBtn.addEventListener("click", handleRestoreResult);
    els.loadScheduleBtn.addEventListener("click", handleLoadSchedule);
    els.resumeFile.addEventListener("change", () => els.resumeFileName.textContent = els.resumeFile.files[0]?.name || "No file selected");
    els.targetRoleInput.addEventListener("input", () => { state.targetRole = els.targetRoleInput.value.trim() || defaults.targetRole; updateSummary(); });
    els.analyzeResumeBtn.addEventListener("click", handleAnalyzeResume);
    els.fetchTrendingBtn.addEventListener("click", handleTrending);
    els.startSkillGapBtn.addEventListener("click", handleSkillGap);
    els.generateAssessmentBtn.addEventListener("click", handleAssessment);
    els.validateLevelBtn.addEventListener("click", handleLevelValidation);
    els.fetchTopicsBtn.addEventListener("click", () => handleTopics());
    els.fetchVideosBtn.addEventListener("click", handleVideos);
    els.generatePracticeBtn.addEventListener("click", handlePractice);
    els.evaluatePracticeBtn.addEventListener("click", handlePracticeEvaluation);
    els.requestHintBtn.addEventListener("click", handleHint);
    els.validateCodeBtn.addEventListener("click", handleValidateCode);
    els.runCodeBtn.addEventListener("click", handleRunCode);
    els.generateScheduleBtn.addEventListener("click", handleSchedule);
    els.completeDayBtn.addEventListener("click", handleCompleteDay);
    els.generateEvaluationBtn.addEventListener("click", handleEvaluation);
    els.scoreEvaluationBtn.addEventListener("click", handleEvaluationScore);
    els.fetchJobsBtn.addEventListener("click", handleJobs);
    els.useManualSkillBtn.addEventListener("click", handleManualSkill);
    els.resetCodeBtn.addEventListener("click", () => { state.code = state.starterCode || ""; renderCode(); save(); });
    els.codeTextarea.addEventListener("input", event => { state.code = event.target.value; save(); });
    els.languageSelect.addEventListener("change", event => { state.currentLanguage = event.target.value; seedStarter(); renderCode(); save(); });
    els.manualSkillInput.addEventListener("keydown", event => {
        if (event.key === "Enter") {
            event.preventDefault();
            handleManualSkill();
        }
    });

    document.querySelectorAll("[data-role-chip]").forEach(btn => btn.addEventListener("click", () => { state.targetRole = btn.dataset.roleChip; els.targetRoleInput.value = state.targetRole; setInitialSelections(); updateSummary(); }));
    document.querySelectorAll("[data-level]").forEach(btn => btn.addEventListener("click", () => { state.declaredLevel = btn.dataset.level; setInitialSelections(); updateSummary(); }));
    document.querySelectorAll("[data-duration]").forEach(btn => btn.addEventListener("click", () => { state.preferredDuration = btn.dataset.duration; setInitialSelections(); save(); }));
    document.querySelectorAll("[data-schedule-toggle]").forEach(btn => btn.addEventListener("click", () => { state.scheduleChoice = btn.dataset.scheduleToggle; setInitialSelections(); renderSchedule(); updateSummary(); }));
    document.querySelectorAll("[data-schedule-mode]").forEach(btn => btn.addEventListener("click", () => { state.scheduleMode = btn.dataset.scheduleMode; setInitialSelections(); renderSchedule(); save(); }));
    document.querySelectorAll("[data-hint-level]").forEach(btn => btn.addEventListener("click", () => { state.hintLevel = Number(btn.dataset.hintLevel); setInitialSelections(); save(); }));

    els.recommendedSkills.addEventListener("click", event => {
        const button = event.target.closest("[data-skill-select]");
        if (button) applySkill(button.dataset.skillSelect);
    });
    els.topicsList.addEventListener("click", event => {
        const select = event.target.closest("[data-topic-select]");
        const replace = event.target.closest("[data-topic-replace]");
        const known = event.target.closest("[data-topic-known]");
        if (select) { state.selectedTopic = select.dataset.topicSelect; render(); return; }
        if (replace) { const topic = replace.dataset.topicReplace; state.excludedTopics = [...new Set([...state.excludedTopics, topic])]; handleTopics([topic]); return; }
        if (known) { const topic = known.dataset.topicKnown; state.knownTopics = [...new Set([...state.knownTopics, topic])]; state.topics = state.topics.filter(item => item !== topic); if (state.selectedTopic === topic) state.selectedTopic = state.topics[0] || null; render(); }
    });
    els.videosList.addEventListener("click", event => {
        const button = event.target.closest("[data-video-select]");
        if (!button) return;
        state.selectedVideo = state.videos[Number(button.dataset.videoSelect)] || null;
        render();
        toast("Video context linked", "Practice generation will now use the selected video context.", "success");
    });
    els.miniLab.addEventListener("click", event => {
        const button = event.target.closest("[data-load-starter]");
        if (!button || !state.practicePack?.mini_lab?.starter_code) return;
        state.code = state.practicePack.mini_lab.starter_code;
        state.starterCode = state.practicePack.mini_lab.starter_code;
        renderCode();
        save();
        els.codePanel?.scrollIntoView({ behavior: "smooth", block: "start" });
        toast("Starter code loaded", "The mini-lab starter code has been placed in the workspace.", "success");
    });

    els.assessmentQuestions.addEventListener("input", event => { const i = Number(event.target.dataset.assessmentIndex); if (!Number.isNaN(i)) { state.assessmentAnswers[i] = event.target.value; save(); } });
    els.assessmentQuestions.addEventListener("change", event => { const i = Number(event.target.dataset.assessmentIndex); if (!Number.isNaN(i)) { state.assessmentAnswers[i] = event.target.value; save(); } });
    els.practiceQuestions.addEventListener("input", event => { const i = Number(event.target.dataset.practiceIndex); if (!Number.isNaN(i)) { state.practiceAnswers[i] = event.target.value; save(); } });
    els.practiceQuestions.addEventListener("change", event => { const i = Number(event.target.dataset.practiceIndex); if (!Number.isNaN(i)) { state.practiceAnswers[i] = event.target.value; save(); } });
    els.evaluationPack.addEventListener("input", event => { const i = Number(event.target.dataset.evaluationIndex); if (!Number.isNaN(i)) { state.evaluationAnswers[i] = event.target.value; save(); } });
    els.evaluationPack.addEventListener("change", event => { const i = Number(event.target.dataset.evaluationIndex); if (!Number.isNaN(i)) { state.evaluationAnswers[i] = event.target.value; save(); } });
}

document.addEventListener("DOMContentLoaded", async () => {
    capture();
    load();
    setInitialSelections();
    bind();
    const initialStage = window.location.hash ? window.location.hash.slice(1) : state.activeStage;
    setActiveStage(initialStage, false);
    render();
    if (state.sessionId) setStatus(els.discoverStatus, `Restored session ${state.sessionId}.`, "success");
    await loadExecutionConfig();
});
