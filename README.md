TUMBLR SCRAPER - EARLY BETA

This is a local Tumblr archiver that saves a public Tumblr blog together with
a bounded, scout-guided neighborhood of related public blogs. Tumblr blogs do
not exist in isolation: observed reblogs and interactions can preserve useful
context if accounts disappear.

This is early beta software intended to help preserve public Tumblr material
during ongoing account loss. Expect rough edges. Keep the original Backups/
directory; updates are designed to resume rather than replace preserved source
records.

It does not need a Tumblr login, account, API key, or upload to a server.
Only public Tumblr material is used. Neighborhood relationships are
observational evidence, not proof of friendship, following, or endorsement.
Public feed availability is imperfect.

QUICK START

1. Download the ZIP or clone this repository.
2. Extract it into a folder.
3. Run the launcher for your platform below.
4. The browser opens the local archive.
5. Enter a public Tumblr blog name.
6. Choose a maximum new-post budget, focus, depth, and network profile.
7. Press Start crawl.
8. Read Blogs, Dashboard, Tags, or Neighborhoods.

Recommended beta settings: Explore, depth 2, Balanced, Gentle, and a bounded
maximum-new-post budget. Balanced splits effort between the target and
surrounding blogs. Depth 2 means target -> nearby -> their nearby blogs.
Gentle uses lower request pressure. Maximum new posts is one total budget
across the whole hunt, not one budget per blog.

ANDROID WITH PYDROID 3

1. Install Pydroid 3 from Google Play.
2. Download Tumblr-Scraper.zip.
3. Open the normal Files app.
4. Tap the ZIP and choose Extract.
5. Open the Tumblr-Scraper folder.
6. Tap Tumblr Scraper - Android.py.
7. Choose Pydroid 3 if Android asks.
8. Press the Run button.

WINDOWS

1. Extract the ZIP.
2. Open the Tumblr-Scraper folder.
3. Double-click Tumblr Scraper - Windows.bat.

MAC

1. Extract the ZIP.
2. Open the Tumblr-Scraper folder.
3. Double-click Tumblr Scraper - macOS.command.

LINUX

1. Extract the ZIP.
2. Open the Tumblr-Scraper folder.
3. Double-click Tumblr Scraper - Linux.desktop.

When the launcher starts, it opens the archive in your normal browser and
waits there for your instructions. Open the collapsed Crawler panel, then
enter the Tumblr blog name, for example:

vigilanceos

Your saved blogs are inside the Backups folder. The browser archive is
available before, during, and after a crawl.

Press Start crawl in the browser. The terminal remains available as a
fallback, but normal use does not require terminal prompts. The archive is
also available later at Backups/index.html. Reading does not require Python,
Tumblr, or a localhost server.

The application boundary and bridge contract are documented in ARCHITECTURE.md.

FIRST-RUN DEPENDENCIES

The release preserves tumblr-backup==1.0.7 and urllib3>=2.2.2,<2.6. If
bundled wheels are not present, the launcher prints "First run: installing
required Tumblr archive components..." and uses pip once with internet access.
This is not a fully offline installer.

DEVELOPMENT TESTING

From the repository root, run:

    python3 -m unittest discover -q

The tests use temporary archive roots and do not require Tumblr network access.





----------------------------------------------------------





MORE INFORMATION

If tapping the Python file does not offer Pydroid, open Pydroid 3, choose
Open, then open Downloads, Tumblr-Scraper, and Tumblr Scraper - Android.py.

Maximum posts:

- Blank means 300 newest posts.
- 1000 means inspect up to 1000 newest posts.
- 0 means no artificial maximum.

Image choice:

- Blank or no uses smaller Tumblr-provided images to save storage and data.
- Yes uses the largest Tumblr-provided image variant available.

The tool does not recompress or alter images.

SURROUNDING PUBLIC CONTEXT:

- Explore saves bounded samples from public blogs observed around the target
  and follows those observations outward within the selected depth.
- The target is the origin of the hunt; canonical archives are top-level
  Backups/<blog>/ holdings.

Context snapshots are samples, not complete backups of those blogs. The
target blog remains the priority, and context work resumes incrementally on
later runs. "Frequently interacting" does not necessarily mean the blogs
follow each other. The tool only knows what it can observe in captured public
posts. It does not know private interactions or follow-mutual status.

Crawl focus:

- Deep concentrates mostly on the target.
- Balanced is the default and steadily reconstructs the neighborhood.
- Wide spreads more effort through nearby and outer blogs while keeping the target highest priority.

The maximum-post value is a total new-post budget across the crawl, not a
separate budget for every blog.

Network aggression:

- Gentle is slower and uses fewer simultaneous requests. It is least likely
  to trigger Tumblr's traffic limits.
- Normal is a balance between speed and request pressure.
- Urgent uses more simultaneous requests and removes most voluntary waiting.
  It may finish faster, but Tumblr is more likely to slow or temporarily
  block requests.

All supplied modes respect Tumblr's server-side throttling requests.

Run the launcher again later to update the library. Existing posts are not
downloaded again unnecessarily. If the program closes or the connection
fails, run it again. Saved work remains on the device and unfinished saved
posts are processed first.

The global archive is stored at:

Tumblr-Scraper/Backups/

The launcher opens the global Blogs page. From there you can open individual
blogs, the global Dashboard, and target-centered neighborhood views.

If Android's built-in file manager makes ordinary folder browsing difficult,
Fossify File Manager from Google Play is an optional free/open-source
alternative. It is not required.

This would not be possible without 
cebtenzzre's fork of tumblr-utils
https://cebtenzzre.github.io/tumblr-utils/

Or without Rael Dornfest's Blosxom
https://www.blosxom.com

Your blog always, always belonged to you,
not the owners of the servers who host it.

