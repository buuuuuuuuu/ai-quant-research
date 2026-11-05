(() => {
  const button = document.getElementById("gold-refresh");
  const status = document.getElementById("gold-status");
  button.addEventListener("click", async () => {
    if (button.disabled || !window.confirm("从上海黄金交易所获取 Au99.99 历史收盘价并更新本地行情？")) return;
    button.disabled = true;
    status.textContent = "正在获取交易所日线…";
    try {
      const response = await fetch("/api/v1/gold/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "更新失败，请稍后重试。");
      if (result.status !== "ok") throw new Error("未收到更新结果，请刷新查看。");
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
})();
