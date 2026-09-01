name: HMR Precise Alert

on:
  workflow_dispatch: {}

permissions:
  contents: write

jobs:
  run-alert:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install requests

      - name: Run precise alert script
        env:
          HMR_TG_TOKEN: ${{ secrets.HMR_TG_TOKEN }}
          HMR_TG_CHAT_ID: ${{ secrets.HMR_TG_CHAT_ID }}
        run: python hmr_precise_alert.py

      - name: Commit updated state
        run: |
          git config user.name "hmr-bot"
          git config user.email "hmr-bot@users.noreply.github.com"
          for i in 1 2 3 4 5; do
            git add state_precise.json
            if git diff --staged --quiet; then
              echo "Tidak ada perubahan"
              break
            fi
            git commit -m "update precise state [skip ci]"
            git pull --rebase origin main
            if git push; then
              break
            fi
            echo "Push gagal, coba lagi ($i)"
            sleep 3
          done
