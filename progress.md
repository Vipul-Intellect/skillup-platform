# Phase 2: Frontend Modernization & Integration Roadmap

*This roadmap has been revised to strictly adhere to the final Phase 2 Implementation Scope prompt.*

---

## 1. Context & Architectural Constraints
**Backend Authority:** The backend is **strictly frozen**. Do NOT modify `api/flask_app.py`, `agents/*`, `tools/*`, `database/*`, Firestore, MCP, Gemini, YouTube backend, executor, AWS, Docker, or requirements.
- **No Load More:** Load More, pagination, cursors, or repeated requests pretending to be pagination will NOT be implemented.
- **Visual Consistency:** The UI must exactly match the existing fonts, colors, buttons, and layout. No generic SaaS styling. **No inline styles**. CSS/HTML will not be changed unless genuinely necessary.

---

## 2. Implementation Execution Plan

### Step 1: YouTube Recommendation UI (Primary Focus)
**Target:** `static/js/app.js` -> `renderVideos()`

1. **Semantic Field Extraction & Mapping:**
   - **`learning_fit_score`**: Extract `video.learning_fit_score`. Display when a valid backend value is present; never fabricate. Use the exact label: `Learning Fit Score`. Wrap in `<span class="tag tag-primary">`. Do NOT calculate, normalize, rank, or modify.
   - **`short_description`**: Extract `video.short_description` and map to an existing class like `<p class="card-meta">` (Never substitute `description` for `short_description`).
   - **`description`**: Extract the full `video.description` and map to `<p class="card-body">`.
   - **`concepts_covered`**: Use the existing `tagList(video.concepts_covered)` to render a `<div class="tag-row">` only when valid values exist.
   - **`why_recommended`**: Render as an unordered list utilizing an existing CSS class for lists (e.g., `<ul class="job-description">`) only when valid values exist.
   
2. **Fallback Logic Separation:**
   - **Retrieval Fallback (`fallback_used`)**: Preserve existing behavior tracking `source === "fallback"`.
   - **Semantic Fallback (`is_semantic_fallback`)**: 
     - Do not present semantic evaluation as successful when true.
     - Render concepts/reasons only when valid backend values actually exist.
     - Do NOT fabricate concepts, reasons, or descriptions.

3. **DOM Security Context:**
   - **HTML Text**: Use context-appropriate escaping (the existing `esc()` function) for safe text handling.
   - **URL Fields**: Validate `video.url` and `video.thumbnail` ensuring they use an expected protocol (`http://` or `https://`) before inserting them into `href` or `src` attributes.
   - **HTML Attributes**: Ensure safe injection into attributes.
   - **Execution Context**: Never place backend values into executable JavaScript or event-handler contexts.

4. **Practice Context Preservation:**
   - Ensure the "Use for Practice" behavior correctly preserves the selected video, skill, level, topic, and video URL/context.
   - Do not alter Monaco/editor behavior.

### Step 2: Dashboard & Evaluation UI
**Target:** `renderEvaluation()`, `renderSchedule()`, `renderGap()`

1. **Data Integrity Audit:**
   - Preserve existing backend-authoritative mappings.
   - Do NOT introduce fabricated progress, percentages, peer rankings, job-readiness metrics, confidence thresholds, or achievements.

---

## 3. End-to-End Testing Matrix

Execute where the required environment is available; otherwise mark Not verified and never claim it passed:
1. [ ] Normal YouTube response
2. [ ] Missing optional fields
3. [ ] Semantic fallback
4. [ ] Retrieval fallback
5. [ ] Watch (URL functionality)
6. [ ] Use for Practice (context preservation)
7. [ ] Existing practice flow
8. [ ] Monaco (editor runs safely)
9. [ ] Evaluation (no fabricated metrics)
10. [ ] Dashboard (data loads accurately)
11. [ ] Responsive layout (no horizontal overflow, uses existing design system)
12. [ ] Malicious backend text/URL rendering (security validation)
13. [ ] Browser console (no unhandled promise errors)

---

## 4. Final Engineering Report
Upon completion, generate a report detailing:
- Files changed
- Exact changes made
- Fields integrated
- Files not changed
- Tests actually executed
- Tests not executed
- Any remaining limitations
