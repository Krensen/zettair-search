# PRD-002: Instant Search (Search-as-you-type)

**Status:** Approved  
**Checkpoint:** Will be part of checkpoint-1

---

## Problem

Users must press Enter or click the Search button to get results. This feels dated. Modern search engines show results as you type.

---

## Goal

Results appear automatically as the user types, with no button press required. The button remains for users who prefer it.

---

## Spec

### Trigger
- Fire a search request after **300ms of inactivity** (debounce)
- Minimum query length: **2 characters** — don't search on single keystrokes
- Do not fire if the query is identical to the last fired query

### Request Handling
- Cancel any in-flight request before firing a new one (use `AbortController`)
- On the results page, the inline search box triggers instant search
- On the home page, instant search does NOT trigger — only fires on submit (to preserve the clean homepage feel)

### Loading State
- Show a subtle spinner or the search icon animating while request is in flight
- Do not clear existing results until new results arrive (prevents flicker)

### Behaviour Details
- Enter key still works (fires immediately, cancels debounce timer)
- Clearing the field completely: hide results, return to clean state
- Backspace/delete: re-fires search with updated query after debounce

---

## Out of Scope
- No dropdown suggestions (that's PRD-005, which Hugh wants to discuss separately)
- No search history
- No keyboard navigation of results

---

## Done When
- [ ] Typing in results search box fires search after 300ms debounce
- [ ] No duplicate requests when typing fast
- [ ] AbortController cancels stale requests
- [ ] Enter key fires immediately
- [ ] Clearing field resets to clean state
- [ ] Home page search box is NOT affected (still requires submit)
- [ ] No visual flickering between result sets
- [ ] Performance: feels snappy, no lag on typical query
