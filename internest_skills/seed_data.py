"""Starter taxonomy + challenge bank. Extend via Django admin; `manage.py seed_skills` is idempotent."""

MCQ, CASE = "mcq", "case"

SKILLS = [
    # ------------------------------------------------------------ Computing
    {
        "name": "Python", "discipline": "computing",
        "aliases": "python, python3, django, flask, fastapi, pandas, numpy, بايثون",
        "implicit": "jupyter notebook, web scraping, scraped data, automation script, data pipeline",
        "sub_skills": {
            "Core syntax & data structures": "python basics, data structures, python fundamentals",
            "Functions & error handling": "python functions, exceptions, clean code",
            "Data handling with pandas": "pandas, data analysis with python, dataframes",
        },
        "items": [
            (MCQ, "Core syntax & data structures", "What is the output of: print(len({1, 2, 2, 3}))", ["4", "3", "2", "Error"], 1, 30),
            (MCQ, "Core syntax & data structures", "Which structure gives average O(1) lookup by key?", ["list", "tuple", "dict", "str"], 2, 30),
            (MCQ, "Functions & error handling", "def f(x, items=[]): items.append(x); return items\nWhat does f(1); f(2) return on the second call?", ["[2]", "[1, 2]", "[1]", "Error"], 1, 45),
            (MCQ, "Functions & error handling", "Which block always runs whether or not an exception occurred?", ["except", "else", "finally", "raise"], 2, 30),
            (MCQ, "Data handling with pandas", "In pandas, which call returns rows where df['age'] > 30?", ["df.where('age' > 30)", "df[df['age'] > 30]", "df.filter(age > 30)", "df.loc('age' > 30)"], 1, 40),
            (CASE, "Data handling with pandas", "A CSV has duplicate customer rows and missing emails. In 2–3 sentences, explain how you would clean it in pandas before analysis.",
             [["drop_duplicates", "duplicate", "duplicates"], ["dropna", "fillna", "missing", "null", "nan"], ["email"]], 60),
        ],
    },
    {
        "name": "SQL", "discipline": "computing",
        "aliases": "sql, mysql, postgresql, postgres, sqlite, t-sql, pl/sql, قواعد البيانات",
        "implicit": "database design, designed a database, wrote queries, reporting queries",
        "sub_skills": {
            "Filtering & joins": "sql joins, sql basics",
            "Aggregation": "group by, sql aggregation, analytics sql",
        },
        "items": [
            (MCQ, "Filtering & joins", "Which JOIN returns all rows from the left table even when there is no match?", ["INNER JOIN", "LEFT JOIN", "CROSS JOIN", "SELF JOIN"], 1, 30),
            (MCQ, "Aggregation", "Which clause filters groups after aggregation?", ["WHERE", "HAVING", "ORDER BY", "LIMIT"], 1, 30),
            (MCQ, "Aggregation", "SELECT COUNT(email) FROM users; counts…", ["all rows", "non-NULL emails", "distinct emails", "NULL emails"], 1, 40),
            (MCQ, "Filtering & joins", "Which predicate correctly finds NULL phone numbers?", ["phone = NULL", "phone IS NULL", "phone == NULL", "ISNULL = phone"], 1, 30),
        ],
    },
    {
        "name": "Web Development", "discipline": "computing",
        "aliases": "javascript, typescript, react, html, css, node.js, nodejs, frontend, backend, rest api, تطوير الويب",
        "implicit": "built a website, developed a website, e-commerce site, landing page, web application",
        "sub_skills": {
            "HTTP & APIs": "rest api, http, web apis",
            "Frontend fundamentals": "html css javascript, frontend development",
        },
        "items": [
            (MCQ, "HTTP & APIs", "Which HTTP status code means the resource was created?", ["200", "201", "301", "404"], 1, 30),
            (MCQ, "HTTP & APIs", "Which method is idempotent and used to fully replace a resource?", ["POST", "PUT", "PATCH", "CONNECT"], 1, 30),
            (MCQ, "Frontend fundamentals", "In JavaScript, what does 0.1 + 0.2 === 0.3 evaluate to?", ["true", "false", "undefined", "TypeError"], 1, 30),
            (MCQ, "Frontend fundamentals", "Which CSS property creates a flex container?", ["display: flex", "flex: 1", "position: flex", "float: flex"], 0, 30),
        ],
    },
    # ------------------------------------------------------------ Business & Finance
    {
        "name": "Financial Analysis", "discipline": "business",
        "aliases": "financial analysis, financial modeling, financial modelling, valuation, dcf, تحليل مالي",
        "implicit": "prepared budgets, budget forecast, investment appraisal, analyzed financial statements, feasibility study, دراسة جدوى",
        "sub_skills": {
            "Time value of money": "npv, irr, discounted cash flow, time value of money",
            "Financial statements": "financial statements, balance sheet, income statement, accounting basics",
            "Ratio analysis": "financial ratios, ratio analysis",
        },
        "items": [
            (MCQ, "Time value of money", "A project's NPV at the company's cost of capital is negative. The standard decision is to…", ["Accept it", "Reject it", "Delay NPV", "Raise prices"], 1, 40),
            (MCQ, "Financial statements", "Depreciation expense appears on which statement?", ["Income statement", "Cash-flow statement only", "Statement of equity only", "None"], 0, 30),
            (MCQ, "Ratio analysis", "Current ratio = ?", ["Current assets / current liabilities", "Total debt / equity", "Net income / sales", "Cash / total assets"], 0, 30),
            (MCQ, "Ratio analysis", "A current ratio of 0.6 most likely signals…", ["Strong liquidity", "Liquidity risk", "High profitability", "Low leverage"], 1, 30),
            (CASE, "Time value of money", "A startup offers you EGP 100,000 today or EGP 115,000 in one year. Inflation and your required return are 20%. Which do you choose and why?",
             [["today", "now", "first"], ["discount", "present value", "pv", "20%"], ["inflation", "required return", "opportunity cost"]], 60),
        ],
    },
    {
        "name": "Excel", "discipline": "business",
        "aliases": "excel, ms excel, microsoft excel, spreadsheets, google sheets, pivot tables, vlookup, اكسل, إكسل",
        "implicit": "built dashboards in spreadsheets, tracked inventory, data entry, reconciliation",
        "sub_skills": {
            "Lookup formulas": "vlookup, xlookup, index match",
            "Pivot tables & summaries": "pivot tables, excel reporting",
        },
        "items": [
            (MCQ, "Lookup formulas", "Which function can look up to the LEFT of the search column without helper columns?", ["VLOOKUP", "XLOOKUP", "HLOOKUP", "SUMIF"], 1, 30),
            (MCQ, "Lookup formulas", "In =VLOOKUP(A2, D:F, 3, FALSE), FALSE means…", ["approximate match", "exact match", "return text", "ignore errors"], 1, 30),
            (MCQ, "Pivot tables & summaries", "To show total sales per region per month you would use…", ["A pivot table", "Conditional formatting", "Data validation", "Freeze panes"], 0, 30),
        ],
    },
    {
        "name": "Digital Marketing", "discipline": "business",
        "aliases": "digital marketing, social media marketing, seo, sem, google ads, meta ads, content marketing, تسويق رقمي, تسويق الكتروني",
        "implicit": "managed social media, grew followers, ran ad campaigns, campaign performance, influencer",
        "sub_skills": {
            "Performance metrics": "marketing metrics, ctr, cac, conversion rate",
            "Campaign strategy": "marketing strategy, target audience, campaign planning",
        },
        "items": [
            (MCQ, "Performance metrics", "1,000 impressions, 50 clicks. What is the CTR?", ["0.5%", "5%", "50%", "20%"], 1, 30),
            (MCQ, "Performance metrics", "CAC is calculated as…", ["Revenue / customers", "Marketing & sales spend / new customers", "Clicks / impressions", "Profit / spend"], 1, 40),
            (CASE, "Campaign strategy", "An Instagram campaign has high reach but almost no sales. List two likely causes and one change you would test.",
             [["audience", "targeting", "segment"], ["landing page", "offer", "call to action", "cta", "price"], ["a/b", "ab test", "test"]], 60),
        ],
    },
    # ------------------------------------------------------------ Media & Design
    {
        "name": "Graphic Design", "discipline": "media",
        "aliases": "graphic design, photoshop, illustrator, adobe illustrator, indesign, canva, figma, branding, تصميم جرافيك",
        "implicit": "designed posters, designed logos, visual identity, brand identity, social media designs",
        "sub_skills": {
            "Design principles": "design principles, visual hierarchy, typography",
            "Color & print production": "color theory, cmyk, print design",
        },
        "items": [
            (MCQ, "Color & print production", "Which color mode should a file for offset print use?", ["RGB", "CMYK", "HSL", "HEX"], 1, 30),
            (MCQ, "Design principles", "Making the headline larger and bolder than body text mainly creates…", ["Contrast & hierarchy", "Repetition", "Alignment", "Proximity"], 0, 30),
            (MCQ, "Color & print production", "Logos should be delivered primarily as…", ["JPEG", "Vector (SVG/AI/EPS)", "GIF", "BMP"], 1, 30),
            (CASE, "Design principles", "A client's flyer feels cluttered. Give two concrete fixes based on design principles.",
             [["white space", "whitespace", "spacing", "padding"], ["hierarchy", "contrast", "font size", "typography"], ["align", "alignment", "grid"]], 60),
        ],
    },
    {
        "name": "UI/UX Design", "discipline": "media",
        "aliases": "ui/ux, ux design, ui design, user experience, figma, wireframes, prototyping, usability testing",
        "implicit": "user interviews, user research, redesigned the app, mobile app design, user flows",
        "sub_skills": {
            "Usability heuristics": "usability, nielsen heuristics, ux principles",
            "Research & testing": "user research, usability testing",
        },
        "items": [
            (MCQ, "Usability heuristics", "Showing a spinner and 'Saving…' after a click addresses which heuristic?", ["Visibility of system status", "Aesthetic design", "Help & documentation", "Flexibility"], 0, 30),
            (MCQ, "Research & testing", "How many users typically uncover most usability issues in a qualitative test round?", ["1", "About 5", "50", "500"], 1, 30),
            (MCQ, "Usability heuristics", "Minimum recommended touch target size on mobile is roughly…", ["12px", "24px", "44–48px", "100px"], 2, 30),
        ],
    },
    {
        "name": "Video Editing", "discipline": "media",
        "aliases": "video editing, premiere pro, after effects, final cut, davinci resolve, capcut, مونتاج",
        "implicit": "edited videos, youtube channel, produced reels, short-form video, motion graphics",
        "sub_skills": {"Editing fundamentals": "video editing basics, premiere pro", "Delivery & formats": "export settings, codecs, aspect ratio"},
        "items": [
            (MCQ, "Delivery & formats", "Standard aspect ratio for Reels / TikTok is…", ["16:9", "4:3", "9:16", "1:1"], 2, 30),
            (MCQ, "Editing fundamentals", "A 'J-cut' means…", ["Audio of next clip starts before its video", "Video fades to black", "Two clips overlap visually", "Cutting on a jump"], 0, 40),
            (MCQ, "Delivery & formats", "Most common web delivery codec is…", ["H.264", "ProRes 4444", "RAW", "DNxHR"], 0, 30),
        ],
    },
    # ------------------------------------------------------------ Law
    {
        "name": "Legal Research", "discipline": "law",
        "aliases": "legal research, case law, legal writing, legal analysis, بحث قانوني",
        "implicit": "moot court, legal memo, drafted memoranda, law firm internship, litigation support",
        "sub_skills": {
            "Sources of law": "sources of law, legal hierarchy, egyptian law",
            "Legal reasoning": "legal reasoning, irac, legal writing",
        },
        "items": [
            (MCQ, "Legal reasoning", "In the IRAC method, 'R' stands for…", ["Rule", "Remedy", "Ruling", "Reason"], 0, 30),
            (MCQ, "Sources of law", "Under the Egyptian legal system, the primary source of law is…", ["Custom", "Legislation", "Judicial precedent", "Doctrine"], 1, 30),
            (CASE, "Legal reasoning", "A client was dismissed without notice after 3 years. Outline, in IRAC form, how you would begin analyzing the claim.",
             [["issue", "whether"], ["rule", "labour law", "labor law", "law no", "article"], ["notice", "compensation", "unfair", "dismissal"], ["conclusion", "therefore", "likely"]], 60),
        ],
    },
    {
        "name": "Contract Drafting", "discipline": "law",
        "aliases": "contract drafting, contracts, drafting agreements, commercial contracts, صياغة العقود",
        "implicit": "drafted contracts, reviewed agreements, nda, memorandum of understanding, mou",
        "sub_skills": {"Core clauses": "contract clauses, termination clause, liability", "Risk allocation": "indemnity, limitation of liability, force majeure"},
        "items": [
            (MCQ, "Risk allocation", "Which clause excuses performance due to extraordinary events beyond control?", ["Indemnity", "Force majeure", "Severability", "Assignment"], 1, 30),
            (MCQ, "Core clauses", "A severability clause ensures that…", ["Invalid terms don't void the whole contract", "Parties can assign rights", "Disputes go to arbitration", "Payment is due in advance"], 0, 40),
            (MCQ, "Risk allocation", "An indemnity clause primarily…", ["Shifts specified losses to one party", "Sets the governing law", "Defines confidentiality", "Ends the contract"], 0, 30),
        ],
    },
    # ------------------------------------------------------------ Transferable
    {
        "name": "Project Management", "discipline": "general",
        "aliases": "project management, agile, scrum, kanban, jira, trello, pmp, إدارة المشاريع",
        "implicit": "led a team, team leader, coordinated, organized an event, student activity, managed a team of",
        "sub_skills": {"Planning & scheduling": "project planning, critical path, gantt", "Agile delivery": "scrum, agile, sprint"},
        "items": [
            (MCQ, "Planning & scheduling", "The critical path is…", ["The shortest path", "The longest sequence of dependent tasks", "The most expensive tasks", "Tasks with most people"], 1, 40),
            (MCQ, "Agile delivery", "In Scrum, who owns and prioritizes the product backlog?", ["Scrum Master", "Product Owner", "Developers", "Stakeholders"], 1, 30),
            (MCQ, "Agile delivery", "A sprint retrospective focuses on…", ["Demoing features", "Improving the team's process", "Estimating the backlog", "Hiring"], 1, 30),
        ],
    },
    {
        "name": "Business Communication", "discipline": "general",
        "aliases": "communication skills, public speaking, presentation skills, business writing, مهارات التواصل",
        "implicit": "presented to, delivered presentations, customer service, call center, wrote reports",
        "sub_skills": {"Professional writing": "business writing, email writing", "Presenting": "presentation skills, public speaking"},
        "items": [
            (MCQ, "Professional writing", "The best subject line for an email requesting a deadline extension is…", ["Hi", "Request: 2-day extension for Q3 report", "URGENT!!!", "Question"], 1, 30),
            (MCQ, "Presenting", "The 'BLUF' principle means…", ["Bottom line up front", "Build long useful frames", "Brief lists under figures", "Better layout, fewer fonts"], 0, 30),
            (CASE, "Professional writing", "Write a 2–3 sentence message to a manager explaining a missed deadline and your recovery plan.",
             [["sorry", "apologize", "apologise", "apologies"], ["new deadline", "by ", "tomorrow", "plan"], ["because", "due to", "reason"]], 60),
        ],
    },
]
