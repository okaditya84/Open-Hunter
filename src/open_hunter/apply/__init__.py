"""Auto-apply agent.

Given the jobs CSV produced by the discovery pipeline, this package opens each
application form with a real browser, fills contact details, uploads the
resume, answers demographic / work-authorization questions from the user's
stated facts, and drafts free-text answers ("why this company?", "best
project") grounded in the resume + that specific job description.

Safety model (chosen by the user): REVIEW BEFORE SUBMIT. The agent fills
everything and screenshots it, then pauses at the Submit button for a
one-click human approval. It never fabricates work-authorization or
demographic answers, and every application is logged with a screenshot.
"""
