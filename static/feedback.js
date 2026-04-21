// 打刻・申請時のフィードバック（バイブ＋音＋視覚）
(function () {
  function vibrate() {
    if (navigator.vibrate) {
      try { navigator.vibrate([60, 40, 60]); } catch (e) {}
    }
  }

  function beep() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      const ctx = new AC();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.type = "sine";
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.18, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
      osc.start();
      osc.stop(ctx.currentTime + 0.18);
    } catch (e) {}
  }

  function showOverlay(mark, msg) {
    const div = document.createElement("div");
    div.className = "feedback-overlay";
    div.innerHTML =
      '<div class="mark">' + mark + "</div>" +
      '<div class="msg">' + msg + "</div>";
    document.body.appendChild(div);
    requestAnimationFrame(() => div.classList.add("show"));
    return div;
  }

  /**
   * フォームの送信時にフィードバック（バイブ・音・✓画面）を表示
   * @param {string} selector - 対象フォームのCSSセレクタ
   * @param {function|object} optsOrFn - {mark,msg} or (event)=>{mark,msg}
   */
  window.attachFeedback = function (selector, optsOrFn) {
    document.querySelectorAll(selector).forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (form.dataset._sent) return;
        form.dataset._sent = "1";
        e.preventDefault();

        const opts = (typeof optsOrFn === "function")
          ? optsOrFn(e) : (optsOrFn || {});

        vibrate();
        beep();
        showOverlay(opts.mark || "✓", opts.msg || "完了");

        setTimeout(function () { form.submit(); }, 450);
      });
    });
  };
})();
