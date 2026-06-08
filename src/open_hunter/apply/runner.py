"""Drive a real browser to fill (and, with approval, submit) applications."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config, PROJECT_ROOT
from ..llm import LLMClient
from ..logging_util import get_logger
from .answers import AnswerEngine
from .forms import ValueResolver, classify, _best_option
from .profile import ApplicantProfile

log = get_logger(__name__)

# JS: tag every fillable control with data-oh-idx and return its metadata.
_EXTRACT_JS = r"""
() => {
  const out = [];
  let i = 0;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const r = document.getElementById(lb);
      if (r && r.innerText) return clean(r.innerText);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText) return clean(l.innerText);
    }
    let p = el.closest('label');
    if (p && p.innerText) return clean(p.innerText);
    // Search the field's container for a label/legend or a question/label-styled
    // element (covers Lever's .application-label, Ashby's field titles, etc.).
    let c = el.closest('div,fieldset,section,li,p');
    const sels = ['label', 'legend', '[class*="label"]', '[class*="question"]',
                  '[class*="title"]', '[class*="Label"]', '[class*="Question"]'];
    for (let hops = 0; c && hops < 4; hops++) {
      for (const s of sels) {
        const lbl = c.querySelector(s);
        if (lbl && clean(lbl.innerText) && lbl !== el &&
            clean(lbl.innerText).length <= 250) {
          return clean(lbl.innerText);
        }
      }
      c = c.parentElement;
    }
    return clean(el.placeholder || el.name || '');
  };
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  document.querySelectorAll('input, textarea, select').forEach((el) => {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden','submit','button','reset','image'].includes(type)) return;
    if (type !== 'file' && !vis(el)) return;
    if (el.disabled || el.readOnly) return;
    el.setAttribute('data-oh-idx', String(i));
    let options = [];
    if (el.tagName.toLowerCase() === 'select') {
      options = Array.from(el.options).map(o => o.text);
    }
    out.push({
      idx: i, tag: el.tagName.toLowerCase(), type,
      name: el.name || '', id: el.id || '',
      label: clean(labelFor(el)).slice(0, 200),
      placeholder: el.placeholder || '',
      value: (el.value || '').slice(0, 200),
      required: !!el.required, options,
    });
    i++;
  });
  // Custom comboboxes / autocompletes (react-select, ARIA listboxes, etc.)
  const comboSel = '[role="combobox"], [aria-haspopup="listbox"], ' +
    '[class*="select__control"], [class*="combobox"], [class*="Select-control"]';
  document.querySelectorAll(comboSel).forEach((el) => {
    if (el.hasAttribute('data-oh-idx')) return;
    if (el.tagName.toLowerCase() === 'select' || !vis(el)) return;
    el.setAttribute('data-oh-idx', String(i));
    out.push({
      idx: i, tag: 'combobox', type: 'combobox',
      name: el.getAttribute('name') || '', id: el.id || '',
      label: clean(labelFor(el)).slice(0, 200), placeholder: '',
      value: clean(el.innerText || '').slice(0, 80),
      required: (el.getAttribute('aria-required') === 'true'), options: [],
    });
    i++;
  });
  return out;
}
"""

_APPLY_BTN = (
    "button:has-text('Apply'), a:has-text('Apply'), "
    "button:has-text('I\\'m interested'), button:has-text('Apply for this job')"
)


def _normalize_apply_url(url: str) -> str:
    u = url.strip()
    if "lever.co" in u and not u.rstrip("/").endswith("/apply"):
        u = u.rstrip("/") + "/apply"
    return u


class ApplyRunner:
    def __init__(self, config: Config, profile: ApplicantProfile,
                 llm: Optional[LLMClient]):
        self.config = config
        self.profile = profile
        self.llm = llm
        self.answers = AnswerEngine(llm, profile)

    def run(self, jobs: List[dict], *, mode: str = "review",
            headless: bool = False, limit: int = 0,
            allow_resubmit: bool = False) -> Path:
        # Skip jobs already submitted in a previous run (resume support) so we
        # never apply to the same posting twice.
        already = set() if allow_resubmit else self._load_ledger()
        if already:
            before = len(jobs)
            jobs = [j for j in jobs if j.get("apply_url") not in already]
            log.info("skipping %d already-applied jobs (ledger has %d)",
                     before - len(jobs), len(already))
        if limit:
            jobs = jobs[:limit]
        if not jobs:
            log.info("nothing left to apply to (all done or filtered out)")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = PROJECT_ROOT / "applications" / stamp
        run_dir.mkdir(parents=True, exist_ok=True)
        master = run_dir / "applications_log.csv"
        self._init_master(master)

        from playwright.sync_api import sync_playwright

        log.info("apply: %d jobs, mode=%s, headless=%s -> %s",
                 len(jobs), mode, headless, run_dir)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=self.config.user_agent,
                viewport={"width": 1366, "height": 1000},
                accept_downloads=False,
            )
            try:
                for n, job in enumerate(jobs, 1):
                    rec = self._apply_one(context, job, run_dir, n, mode)
                    self._append_master(master, rec)
                    if str(rec["status"]).startswith("SUBMITTED"):
                        self._append_ledger(job)
                    log.info("[%d/%d] %s :: %s -> %s",
                             n, len(jobs), job.get("company_name", ""),
                             (job.get("title", "") or "")[:40], rec["status"])
            except KeyboardInterrupt:
                log.info("stopped by user; progress saved. "
                         "Re-run the same command to resume (applied jobs are "
                         "skipped automatically).")
            context.close()
            browser.close()
        log.info("apply run complete -> %s", run_dir)
        return run_dir

    # --- persistent ledger (resume + no double-applies) --------------- #
    def _ledger_path(self) -> Path:
        return PROJECT_ROOT / "applications" / "applied_ledger.csv"

    def _load_ledger(self) -> set:
        import csv
        p = self._ledger_path()
        if not p.exists():
            return set()
        urls = set()
        try:
            for row in csv.DictReader(open(p, encoding="utf-8-sig")):
                if row.get("apply_url"):
                    urls.add(row["apply_url"])
        except Exception:  # noqa: BLE001
            pass
        return urls

    def _append_ledger(self, job: dict) -> None:
        import csv
        p = self._ledger_path()
        new = not p.exists()
        with p.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["apply_url", "company", "title", "submitted_at"])
            w.writerow([job.get("apply_url", ""), job.get("company_name", ""),
                        job.get("title", ""),
                        datetime.now().isoformat(timespec="seconds")])

    # ------------------------------------------------------------------ #
    def _apply_one(self, context, job, run_dir, n, mode) -> dict:
        company = (job.get("company_name") or "co").replace("/", "-")
        jid = (job.get("job_id") or str(n)).replace("/", "-")
        app_dir = run_dir / f"{n:02d}_{company}_{jid}"[:80]
        app_dir.mkdir(exist_ok=True)
        url = _normalize_apply_url(job.get("apply_url", ""))
        rec = {"company": job.get("company_name", ""), "title": job.get("title", ""),
               "url": url, "status": "error", "filled": 0, "skipped_for_review": 0,
               "dir": str(app_dir)}
        if not url:
            rec["status"] = "no_url"
            return rec

        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=int(self.config.http_timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass

            # Reveal the form if it's behind an Apply button.
            if not page.query_selector("input, textarea, select"):
                try:
                    page.locator(_APPLY_BTN).first.click(timeout=4000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass

            self._upload_resume(page)
            filled, review_items = self._fill_fields(page, job)
            rec["filled"] = len(filled)
            rec["skipped_for_review"] = len(review_items)

            page.screenshot(path=str(app_dir / "filled_form.png"), full_page=True)
            vision = self._vision_qa(page, app_dir)
            if vision:
                rec["vision"] = vision
            (app_dir / "answers.json").write_text(
                json.dumps({"job": {k: job.get(k) for k in
                                    ("company_name", "title", "apply_url", "location")},
                            "filled": filled, "needs_review": review_items,
                            "vision_qa": vision,
                            "unconfirmed_profile_fields": self.profile.unconfirmed_fields()},
                           indent=2, ensure_ascii=False), encoding="utf-8")

            rec["status"] = self._finish(page, mode, app_dir, rec)
        except Exception as exc:  # noqa: BLE001
            rec["status"] = f"error: {str(exc)[:120]}"
            try:
                page.screenshot(path=str(app_dir / "error.png"), full_page=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            if mode != "review":
                page.close()
        return rec

    def _upload_resume(self, page) -> None:
        rp = self.profile.resume_path
        if not rp:
            return
        for fi in page.query_selector_all("input[type=file]"):
            try:
                fi.set_input_files(str(rp), timeout=4000)
                page.wait_for_timeout(1500)
                log.debug("resume uploaded via a file input")
                return
            except Exception:  # noqa: BLE001
                continue

    def _fill_fields(self, page, job):
        resolver = ValueResolver(self.profile, self.answers, job)
        meta = page.evaluate(_EXTRACT_JS)
        filled, review = [], []
        for m in meta:
            if m["type"] == "file":
                continue
            label = m["label"]
            cat = classify(label, m["name"], m["placeholder"])
            sel = f'[data-oh-idx="{m["idx"]}"]'
            try:
                if m["tag"] == "select":
                    if _has_value_select(m):
                        continue
                    desired = resolver.combo_desired(cat, label)
                    if desired is None:
                        if m["required"]:
                            review.append({"label": label, "cat": cat,
                                           "options": m["options"][:8]})
                        continue
                    # Flexible: let the LLM map intent -> actual option text.
                    val = (self.answers.pick_option(label, desired, m["options"])
                           or _best_option(m["options"], [desired]))
                    if val:
                        page.select_option(sel, label=val, timeout=3000)
                        filled.append({"label": label, "cat": cat, "value": val})
                    elif m["required"]:
                        review.append({"label": label, "cat": cat,
                                       "options": m["options"][:8]})
                elif m["tag"] == "combobox":
                    if _combo_has_value(m):
                        continue
                    desired = resolver.combo_desired(cat, label)
                    if desired and self._fill_combobox(page, sel, desired, label):
                        filled.append({"label": label, "cat": cat or "dropdown",
                                       "value": desired})
                    else:
                        review.append({"label": label, "cat": cat or "dropdown",
                                       "desired": desired or "(needs your choice)"})
                else:
                    if m["value"].strip():
                        continue  # already filled, don't overwrite
                    val = resolver.text_value(cat, label)
                    # Any free-text box (textarea) is almost always an essay /
                    # custom question — draft a grounded answer from its label.
                    if not val and m["tag"] == "textarea" and len(label) >= 6:
                        val = self.answers.answer(label, job) or None
                    if val:
                        page.fill(sel, val, timeout=3000)
                        filled.append({"label": label, "cat": cat or "free_text",
                                       "value": val[:200]})
                    elif m["required"] or cat in ("why", "cover_letter") \
                            or m["tag"] == "textarea":
                        review.append({"label": label, "cat": cat or "free_text"})
            except Exception as exc:  # noqa: BLE001
                review.append({"label": label, "cat": cat,
                               "error": str(exc)[:80]})
        return filled, review

    def _fill_combobox(self, page, sel, value, label="") -> bool:
        """Open a custom dropdown/autocomplete, type, then click the best option.

        Selection is flexible: we scrape the visible options and let the LLM map
        the intended answer to the right one (handles wording differences), so
        it's not rigid substring matching.
        """
        loc = page.locator(sel).first
        try:
            loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.click(timeout=3000)
        except Exception:  # noqa: BLE001
            return False
        page.wait_for_timeout(400)
        # Type only the leading token for autocompletes (e.g. city), so the
        # list isn't over-filtered; full phrases for plain selects are fine too.
        try:
            page.keyboard.type(value.split(",")[0].strip(), delay=30)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1200)

        for opt_sel in ('[role="option"]', '[class*="select__option"]',
                        'li[role="option"]', '[class*="option"]',
                        '[class*="menu"] li'):
            opts = page.locator(opt_sel)
            try:
                cnt = min(opts.count(), 20)
            except Exception:  # noqa: BLE001
                cnt = 0
            if not cnt:
                continue
            # Gather visible option texts.
            texts, handles = [], []
            for k in range(cnt):
                o = opts.nth(k)
                try:
                    if not o.is_visible():
                        continue
                    t = (o.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if t:
                    texts.append(t)
                    handles.append(o)
            if not texts:
                continue
            # Flexible match: LLM picks the best option text, else key match.
            choice = self.answers.pick_option(label or "", value, texts)
            target = None
            if choice and choice in texts:
                target = handles[texts.index(choice)]
            if target is None:
                key = value.lower().split(",")[0].strip()
                for t, h in zip(texts, handles):
                    if key and (key in t.lower() or t.lower() in key):
                        target = h
                        break
            if target is None:
                target = handles[0]
            try:
                target.click(timeout=2000)
                return True
            except Exception:  # noqa: BLE001
                pass
        try:
            page.keyboard.press("Enter")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _vision_qa(self, page, app_dir) -> dict:
        """Ask the vision model to spot empty required fields / unset dropdowns."""
        if not (self.config.enable_vision and self.llm
                and self.llm.vision_available):
            return {}
        shot = app_dir / "for_vision.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:  # noqa: BLE001
            return {}
        prompt = (
            "This is a screenshot of a job-application form being auto-filled. "
            "Identify problems a human should fix before submitting. Respond "
            "ONLY with JSON: {\"empty_required\": [field labels that are "
            "required but appear blank], \"unset_dropdowns\": [dropdowns still "
            "showing a placeholder like 'Select...'], \"notes\": \"one line\"}. "
            "If everything important looks filled, return empty lists."
        )
        try:
            res = self.llm.vision_json(prompt, str(shot), self.config.vision_model)
            return res if isinstance(res, dict) else {}
        except Exception as exc:  # noqa: BLE001
            log.debug("vision QA failed: %s", exc)
            return {}

    def _finish(self, page, mode, app_dir, rec) -> str:
        if mode == "draft":
            return "drafted (not submitted)"
        if mode == "auto":
            return self._click_submit(page, app_dir)
        # review: pause for the human running in their terminal.
        print("\n" + "=" * 70)
        print(f"REVIEW: {rec['company']} — {rec['title']}")
        print(f"  URL: {rec['url']}")
        print(f"  filled {rec['filled']} fields; {rec['skipped_for_review']} need your eyes")
        v = rec.get("vision") or {}
        if v.get("empty_required") or v.get("unset_dropdowns"):
            print(f"  👁 vision check — empty required: {v.get('empty_required')}")
            print(f"  👁 vision check — unset dropdowns: {v.get('unset_dropdowns')}")
        todo = self.profile.unconfirmed_fields()
        if todo:
            print(f"  ⚠ unconfirmed profile fields: {', '.join(todo)}")
        print("  Look at the open browser. Then:")
        ans = input("  [Enter]=SUBMIT   s=skip   q=quit run : ").strip().lower()
        if ans == "q":
            raise KeyboardInterrupt("user quit")
        if ans == "s":
            page.close()
            return "skipped by user"
        result = self._click_submit(page, app_dir)
        page.close()
        return result

    def _click_submit(self, page, app_dir) -> str:
        for sel in ("button:has-text('Submit application')",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "input[type=submit]"):
            try:
                page.locator(sel).first.click(timeout=4000)
                page.wait_for_timeout(3000)
                page.screenshot(path=str(app_dir / "after_submit.png"),
                                full_page=True)
                return "SUBMITTED"
            except Exception:  # noqa: BLE001
                continue
        return "submit_button_not_found"

    # ------------------------------------------------------------------ #
    def _init_master(self, path: Path) -> None:
        import csv
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                ["company", "title", "status", "filled",
                 "needs_review", "url", "dir"])

    def _append_master(self, path: Path, rec: dict) -> None:
        import csv
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([
                rec["company"], rec["title"], rec["status"], rec.get("filled", 0),
                rec.get("skipped_for_review", 0), rec["url"], rec["dir"]])


def _has_value_select(m: dict) -> bool:
    v = (m.get("value") or "").strip().lower()
    return v not in ("", "select", "select...", "please select", "--")


def _combo_has_value(m: dict) -> bool:
    v = (m.get("value") or "").strip().lower()
    return v not in ("", "select", "select...", "please select", "choose",
                     "choose...", "select an option", "--", "search...")
