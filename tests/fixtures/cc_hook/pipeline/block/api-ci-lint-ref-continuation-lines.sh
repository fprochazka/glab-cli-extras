glab api "projects/group%2Fproject/ci/lint?ref=feature/branch&dry_run=true&include_jobs=true" \
  --hostname gitlab.example.com > /tmp/work/scratchpad/lint-result.json 2>&1
echo "exit=$?"
