# AI Web Tester for Dummies — User Guide

> 🇹🇭 ภาษาไทย: see **[QA_GUIDE.th.md](QA_GUIDE.th.md)**

A simple tool that uses AI to test a website like a real person would — it opens
the site in a browser, follows the steps you describe in plain language, and gives
you a report with screenshots of every step.

**You do not need to know any programming.** Everything happens in your web browser.
The interface is available in **Thai and English** — use the **ไทย / EN** switch in
the top-right corner. It starts in Thai.

---

## 1. First-time setup (once per computer)

### macOS
1. Double-click **`Start (macOS).command`**.
2. If macOS says *"cannot be opened because it is from an unidentified developer"*:
   - **Right-click** the file → **Open** → **Open** again. (You only do this once.)
3. A black window opens and installs everything it needs. **This takes a few
   minutes and downloads a few hundred MB** — please wait. Keep the window open.

### Windows
1. Double-click **`Start (Windows).bat`**.
2. If Windows shows a blue *"Windows protected your PC"* box:
   - Click **More info** → **Run anyway**. (You only do this once.)
3. A black window opens and installs everything it needs. **This takes a few
   minutes** — please wait. Keep the window open.

> 💡 You need an internet connection the first time. After setup, starting the
> tool is fast.

---

## 2. Enter your Gemini API key (once)

The tool uses Google Gemini AI. Each person uses their **own free key**.

1. When your browser opens, you'll see a **Welcome** screen.
2. Get a key at **https://aistudio.google.com/apikey** (sign in with Google →
   *Create API key* → copy it).
3. Paste the key and click **Save and continue**.

Your key is stored only on your computer and is never shared.

---

## 3. Point it at your website (once)

1. Click **⚙ Settings**.
2. Set **Base URL** to the website you want to test (e.g. `https://your-site.com`).
3. *(Optional)* If your site needs a login, paste a **Login URL** that signs you
   in, and tick **Start at Login URL first** on the tests that need it.
4. *(Optional)* Add a **Context hint** that applies to every test, e.g.
   *"This is a mobile site. Dismiss any cookie banner first."*
5. Click **Save**.

---

## 4. Everyday use

1. Double-click the **Start** file for your OS. Your browser opens to the tester.
   *(Keep the black window open while you work.)*
2. You'll see a list of test cases.
3. Tick the ones you want and click **▶ Run selected**, or click **▶ Run all**.
   - Tick **Show browser window** if you'd like to watch it work.
4. Watch the progress bars. When it's done, the **Report** appears right below —
   scroll through to see each step's screenshot and result. Click any screenshot
   to enlarge it. Use **Open in new tab** to view it full-screen, or to save/share.

---

## 5. Create your own test

1. Click **＋ New test**.
2. Fill in:
   - **Name** — a short id, lowercase, no spaces (e.g. `add_to_cart`).
   - **Title** — a friendly name (e.g. *Add a product to cart*).
   - **What should the test do?** — describe it in plain English, e.g.:
     > *Open the first product, click "Add to cart", then open the cart and
     > confirm the product is listed.*
3. Click **Save**. You don't need to mention opening the site — that's automatic.
4. Run it like any other test.

You can **Edit** or **🗑 delete** any test from its row.

---

## 6. Tips & troubleshooting

- **"A run is already in progress"** — only one run happens at a time. Wait for
  it to finish.
- **Tests start on the wrong page** — check the **Base URL** in **⚙ Settings**.
- **Login isn't working** — update the **Login URL** in **⚙ Settings**, and make
  sure the test has **Start at Login URL first** ticked.
- **Nothing happened after double-click (first time)** — the install is still
  running in the black window; give it a few minutes.
- **Tests are slow or a step is wrong** — in **⚙ Settings**, switch the model to
  `gemini-2.5-pro` (slower but smarter).
- **Start over / something is broken** — delete the `.venv` folder next to the
  Start file, then double-click Start again to reinstall.
- **To stop the tool** — close the black window (or press Ctrl+C in it).
