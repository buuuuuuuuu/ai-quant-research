/* Reference profiles, reviewed 2026-11-06. These are not portfolio snapshots.
 * Inception: Eastmoney fund basic profiles (jbgk_<code>.html), linked per card.
 * Index rules: the methodology/disclosure sources linked below.
 */
(() => {
  const reviewed = "2026-11-06";
  const rows = [
    ["000300", "SSE.510300", "华泰柏瑞沪深300ETF", "华泰柏瑞基金", "2012-05-04"],
    ["000300", "SZSE.159919", "嘉实沪深300ETF", "嘉实基金", "2012-05-07"],
    ["000300", "SSE.510310", "易方达沪深300ETF", "易方达基金", "2013-03-06"],
    ["000300", "SSE.510330", "华夏沪深300ETF", "华夏基金", "2012-12-25"],
    ["000852", "SSE.512100", "南方中证1000ETF", "南方基金", "2016-09-29"],
    ["000852", "SZSE.159845", "华夏中证1000ETF", "华夏基金", "2021-03-18"],
    ["000852", "SSE.560010", "广发中证1000ETF", "广发基金", "2022-07-28"],
    ["932000", "SSE.563300", "华泰柏瑞中证2000ETF", "华泰柏瑞基金", "2023-09-06"],
    ["399006", "SZSE.159915", "易方达创业板ETF", "易方达基金", "2011-09-20"],
    ["000905", "SSE.510500", "南方中证500ETF", "南方基金", "2013-02-06"],
    ["000905", "SSE.510510", "广发中证500ETF", "广发基金", "2013-04-11"],
    ["000905", "SSE.512500", "华夏中证500ETF", "华夏基金", "2015-05-05"],
    ["000905", "SZSE.159922", "嘉实中证500ETF", "嘉实基金", "2013-02-06"],
    ["000905", "SSE.510580", "易方达中证500ETF", "易方达基金", "2015-08-27"],
    ["000510", "SZSE.159351", "嘉实中证A500ETF", "嘉实基金", "2024-09-20"],
    ["000016", "SSE.510050", "华夏上证50ETF", "华夏基金", "2004-12-30"],
    ["000016", "SSE.510100", "易方达上证50ETF", "易方达基金", "2019-09-06"],
    ["000688", "SSE.588000", "华夏科创50ETF", "华夏基金", "2020-09-28"],
    ["000688", "SSE.588080", "易方达科创50ETF", "易方达基金", "2020-09-28"],
  ];
  const methodology = code => `https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/${code}_Index_Methodology_cn.pdf`;
  const indices = {
    "000300": {name: "沪深300", universe: "主要投资沪深300的成分股和备选成分股，覆盖沪深市场规模较大、流动性较好的300只证券。", source: methodology("000300")},
    "000905": {name: "中证500", universe: "主要投资中证500的成分股和备选成分股，关注沪深300之外的一组中小市值公司。它与中证A500的选样规则不同。", source: methodology("000904")},
    "000852": {name: "中证1000", universe: "主要投资中证1000的成分股和备选成分股，指数选取中证800之外规模偏小、流动性较好的1000只证券。", source: methodology("000852")},
    "932000": {name: "中证2000", universe: "主要投资中证2000的成分股和备选成分股，指数选取中证800和中证1000之外的一组较小市值证券，共2000只。", source: "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-06-28/562660_20240628_K97M.pdf"},
    "000510": {name: "中证A500", universe: "主要投资中证A500的成分股和备选成分股，指数从各行业选取市值较大、流动性较好的500只证券，兼顾行业分布。它不是中证500。", source: methodology("000510")},
    "000016": {name: "上证50", universe: "主要投资上证50的成分股和备选成分股，覆盖上海证券市场规模大、流动性好的50只代表性证券。", source: "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-02-09/510050_20260209_5FIR.pdf"},
    "399006": {name: "创业板指", universe: "主要投资创业板指的成分股和备选成分股，指数由创业板市场市值大、流动性好的100只股票组成。", source: "https://www.cnindex.com.cn/module/index-detail.html?activeName=2&indexCode=399006"},
    "000688": {name: "科创50", universe: "主要投资科创50的成分股和备选成分股，覆盖科创板市值大、流动性好的50只证券。", source: "https://www.sse.com.cn/market/sseindex/indexlist/indexdetails/indexmethods/c1/000688_000688hbook_CN.pdf"},
  };
  const catalog = new Map(rows.map(([index, code, name, manager, inception]) => [code, {index, code, name, manager, inception}]));
  const examples = {};
  for (const fund of catalog.values()) (examples[fund.index] ||= []).push([fund.code, fund.name]);

  function beijingDay(now = new Date()) {
    const parts = new Intl.DateTimeFormat("en-US", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(now);
    const part = type => parts.find(item => item.type === type).value;
    return `${part("year")}-${part("month")}-${part("day")}`;
  }
  function age(inception, today = beijingDay()) {
    const valid = value => /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value;
    if (!valid(inception) || !valid(today)) return "存续时间待核实";
    if (inception > today) return "该日期尚未成立";
    const start = inception.split("-").map(Number), end = today.split("-").map(Number);
    let months = (end[0] - start[0]) * 12 + end[1] - start[1];
    if (end[2] < start[2]) months--;
    if (months < 1) return "成立不足1个月";
    return `已满${Math.floor(months / 12)}年${months % 12}个月`;
  }
  function render(container, selectedIndex, selectedCodes = []) {
    const openCodes = new Set([...container.children].filter(node => node.open).map(node => node.getAttribute("data-fund-code")));
    container.replaceChildren();
    const codes = [...new Set([...(examples[selectedIndex] || []).map(([code]) => code), ...selectedCodes])];
    const today = beijingDay();
    function element(tag, text) {
      const node = document.createElement(tag); node.textContent = text; return node;
    }
    function link(parent, label, href) {
      const node = element("a", label); node.href = href; node.target = "_blank";
      node.rel = "noopener noreferrer"; parent.append(node);
    }
    for (const code of codes) {
      const fund = catalog.get(code), index = fund && indices[fund.index];
      const card = document.createElement("details");
      card.className = "fund-profile"; card.setAttribute("data-fund-code", code);
      card.open = openCodes.has(code);
      const summary = element("summary", fund ? `${fund.name} · ${code}` : `${code}（资料待核实）`);
      card.append(summary);
      const body = document.createElement("div"); body.className = "fund-profile-body";
      const facts = document.createElement("dl");
      function fact(label, value) { facts.append(element("dt", label), element("dd", value)); }
      if (fund) {
        fact("成立多久", `${fund.inception} 成立 · ${age(fund.inception, today)}（截至 ${today}，北京时间；不是上市日期）`);
        fact("谁在管理", fund.manager + " · 股票指数 ETF");
        fact("跟踪什么", index.name + "（" + fund.index + "）" + (fund.index !== selectedIndex ? "；与当前所选指数不同，请核对基金选择。" : ""));
        fact("主要买什么", index.universe + "具体股票和比例会变化，请查看下方披露持仓。");
        fact("指数何时调样", fund.index === "000688" ? "每季度一次，通常在3、6、9、12月第二个星期五的下一交易日实施；也可能临时调整。" : "每半年一次，通常在6、12月第二个星期五的下一交易日实施；也可能临时调整。");
        fact("基金怎样调仓", "以跟踪指数为目标，随成分股及权重变化调整组合；申购赎回、分红、停牌等也可能引起持仓变化。指数调样日不等于基金唯一交易日，实际操作以基金公告和报告为准。");
      } else {
        fact("资料状态", "暂未收录该基金的名称、成立日期、跟踪指数和投资策略。手动填写不代表已核实其跟踪当前指数。");
      }
      fact("具体股票与比例", "本页尚未收录持仓报告。下方“披露持仓”可查股票名称和占净值比例；请核对报告期，已披露持仓不等于实时持仓。");
      fact("历史买卖", "“披露买卖变动”提供报告期内部分累计买卖明细，不是每日交易流水，也不能还原每次调仓。");
      body.append(facts);
      const links = document.createElement("div"); links.className = "fund-profile-links";
      if (/^(SSE|SZSE)\.\d{6}$/.test(code)) {
        const number = code.split(".")[1];
        for (const [label, page] of [["基本资料", "jbgk"], ["披露持仓", "ccmx"], ["披露买卖变动", "ccbd"], ["基金公告与报告", "jjgg"], ["费率", "jjfl"]]) {
          link(links, label + " ↗", `https://fundf10.eastmoney.com/${page}_${number}.html`);
        }
      }
      if (index) link(links, "指数规则来源 ↗", index.source);
      body.append(links);
      body.append(element("p", (fund ? `资料核对日 ${reviewed}。成立日期来自天天基金基本资料，指数规则来自编制机构或交易所披露；` : "未核实代码的外部页面可能无资料；") + "以上基金查询入口为天天基金，打开后离开本系统。"));
      card.append(body); container.append(card);
    }
  }
  window.FundProfiles = {examples, render, age, beijingDay};
})();
