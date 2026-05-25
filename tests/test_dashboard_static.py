from pathlib import Path


def test_dashboard_comments_show_author_and_time_without_emotion_fields():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert "var commentTime = c.original_feel_created || c.created || '';" in html
    assert "let dashboardAiAuthor = 'Haven';" in html
    assert "function commentAuthorName(comment)" in html
    assert '<div class="comment-meta">' in html
    assert '<span class="comment-author">' in html
    assert '<span class="comment-time">' in html

    comments_block = html.split("var commentsHtml = comments.length", 1)[1].split("var commentFormHtml", 1)[0]
    assert "commentAuthorName(c)" in comments_block
    assert "c.valence" not in comments_block
    assert "c.arousal" not in comments_block


def test_dashboard_comment_enter_submit_has_no_visible_send_key():
    html = Path("dashboard.html").read_text(encoding="utf-8")
    form_block = html.split("var commentFormHtml =", 1)[1].split("content.innerHTML =", 1)[0]

    assert "handleCommentKeydown(event)" in form_block
    assert "comment-send-button" not in html
    assert 'aria-label="发送"' not in form_block


def test_dashboard_exposes_gateway_memory_cooldown_settings():
    html = Path("dashboard.html").read_text(encoding="utf-8")

    assert "<h3>记忆浮现</h3>" in html
    assert 'id="cfg-gateway-cooldown"' in html
    assert 'id="cfg-gateway-rounds"' in html
    assert "cfg.gateway.cooldown_hours" in html
    assert "cfg.gateway.skip_recent_rounds" in html
    assert "cooldown_hours: floatValue('cfg-gateway-cooldown', 6)" in html
    assert "skip_recent_rounds: numberValue('cfg-gateway-rounds', 5)" in html
