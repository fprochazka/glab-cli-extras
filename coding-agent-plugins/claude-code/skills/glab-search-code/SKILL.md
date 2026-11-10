---
name: glab:glab-search-code
description: >-
  This skill should be used when the user asks to "search code across GitLab", "find every usage of X
  in all repos", "which projects use X", "grep all of GitLab", "find where this class or table is
  used", or needs to find code in GitLab projects that are not cloned locally. Covers
  `glab-search-code`, the files it downloads, and the order to read them in.
allowed-tools: Bash(glab-search-code:*)
---

# glab-search-code

`glab-search-code` runs a GitLab code search and downloads every file that matched, whole, into a new dir. Then you read and grep the files locally instead of paging through search results. It comes with glab-cli-extras (`uv tool install glab-cli-extras`).

Use it instead of `glab api "search?scope=blobs…"`. A raw blob search returns 20 hits per page and only the matched lines. For a quick look at one page that is fine, but never draw a conclusion such as "nothing else uses it" from one page.

## Running it

```bash
glab-search-code 'GeneratedValue'                                # the whole instance of the current repository's host
glab-search-code 'GeneratedValue' --hostname gitlab.example.com  # another host, or outside a repository
glab-search-code 'class MyService' --group group                 # one group and its subgroups
glab-search-code 'class MyService' --project group/project       # one project
glab-search-code 'GeneratedValue' --workers 20                   # more requests at once (1 to 50, default 10)
```

- **The host** defaults to the current directory's repository. Outside a repository, pass `--hostname`.
- **A search across a group or the instance needs Advanced search or Exact code search** on that GitLab. Without it, GitLab answers 400 and the command exits 1 and says to pass `--project`. Then search the projects you suspect one by one with `--project`.
- **One query per run.** Each run writes its own dir. For several spellings of a name, run the command once per spelling, one after another or as separate background tasks. Never start several runs with `&` and `wait` in one Bash call: the outputs interleave and a failure of one run is lost.
- **Progress goes to stderr.** Stdout ends with two lines, `Output directory: <dir>` and `Metadata file: <file>`, also when nothing matched. Take the paths from those lines; never build them.
- **The exit code is 0 when the search ran**, also when some downloads failed. The summary then says `Failed downloads: N`, and `download.log` has a `FAIL:` line for each, with the reason.
- **More than 10,000 hits** stops the search, and the summary says `Search stopped after 10000 hits`. Narrow the query, or search a group or a project.

## Reading the results

Read in this order, and stop as soon as you have the answer:

1. **`hits.json`** for the overview: every match with `project_path`, `path`, `ref`, `startline` and `data`, the matched lines. `jq` answers "which projects" without opening a file:

   ```bash
   jq -r '.[].project_path' <dir>/hits.json | sort | uniq -c | sort -rn
   ```

2. **`metadata.json`** for the downloaded files: `project_path`, `file_path`, `ref`, `output_file` (relative to the dir), `url` (the file on GitLab, for links in a report) and `hits` (the start lines of the matches).
3. **`rg` over `<dir>/files/`** for context around the matches, or for a narrower pattern. The tree is `files/<project path>/<file path>`, so a path in the output is also the file's path in its repository.
4. **Single files** only after that, at the lines `hits` names.

GitLab's search is not grep. Depending on the instance's search backend, a hit can match only a part of the query, and a file that contains the text can be missing. Check a hit in the file before you report it, and report "the search found no use" rather than "there is no use".

## What the files are

The files are a snapshot of the ref the search ran on, usually each project's default branch, at the time of the run. Other branches and open merge requests are not searched. When a local clone of a project exists, it is the source of truth: check the hit there before you change code because of it. Run the search again rather than reuse an old run's dir.
