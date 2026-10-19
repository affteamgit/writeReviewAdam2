"""
review_export.py - TEMPORARY (2026-10-19). Remove once the BCK review-comments
extraction has its data.

The WordPress DB only accepts connections from the deployed app's network, so the
one-off extraction (run locally) gets its review texts through here: a hidden page,
reachable only after the app's password login and with ?export=bck-reviews in the URL,
that runs one read-only SELECT for every post listed in WP_IDs and offers the result
as a JSON download. Nothing is written anywhere.
"""

from __future__ import annotations

import json

import streamlit as st

import casino_data as cd
import config


def render() -> None:
    st.title("BCK review export (temporary)")
    st.caption("One read-only query for every review post listed in the WP_IDs tab.")
    if not st.button("Fetch reviews"):
        return

    rows = cd._fetch(cd._sheets_client(), "WP_IDs!A:B",
                     config.get("CALCULATION_SPREADSHEET_ID"))
    targets = {r[1].strip(): r[0].strip() for r in rows[1:]
               if len(r) >= 2 and r[1].strip().isdigit()}
    cfg = config.mysql_config()
    if not cfg:
        st.error("MySQL is not configured in this app's secrets.")
        return

    import pymysql
    conn = pymysql.connect(
        host=cfg["host"], port=int(cfg.get("port", 3306)), user=cfg["user"],
        password=cfg["password"], database=cfg["database"], connect_timeout=10,
        read_timeout=60, cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(targets))
            cur.execute(
                f"SELECT ID, post_title, post_name, post_status, post_modified, post_content "
                f"FROM wp_posts WHERE ID IN ({ph})", [int(i) for i in targets])
            posts = cur.fetchall()
    finally:
        conn.close()

    for p in posts:
        p["casino_id"] = targets.get(str(p["ID"]), "")
    st.success(f"Fetched {len(posts)} of {len(targets)} posts.")
    st.download_button("Download bck_reviews.json",
                       json.dumps(posts, default=str, ensure_ascii=False),
                       file_name="bck_reviews.json", mime="application/json")
