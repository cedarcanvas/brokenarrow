# USPS service-standard files go here

**Usually you don't need to do anything:** the GitHub Action runs
`build/fetch_usps.py`, which downloads the newest files from PostalPro
automatically. Add files here by hand only if that fails (the build log
will say so), or to pin a specific quarter. Files here always win.

To add them by hand:

1. Open https://postalpro.usps.com/operations/service-standards
2. Download the current service-standard files for the mail classes you want:
   First-Class Mail, USPS Ground Advantage, Priority Mail, USPS Marketing Mail,
   Periodicals. The base files list days for every 3-digit origin and
   destination prefix. The file layouts are described at
   https://postalpro.usps.com/SSD_file_layouts
3. Drop the downloads in this folder as they are (`.zip`, `.txt`, `.csv`
   or `.xlsx`).
4. Optional: put the data date (for example `FY2027 Q1`) in `vintage.txt`. It
   appears in the page footer.

Then run `python build/build_data.py`, or just push to `main` and let the
GitHub Action build it.

GitHub refuses single files over 100 MB. If a download is bigger, keep only
the classes you need, or ask about Git LFS.
