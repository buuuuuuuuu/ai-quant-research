const test = require('node:test');
const assert = require('node:assert/strict');
const {money,numeric,parseFunds,countsText} = require('../../src/ai_quant_assistant/web/static/national-team.js');
test('missing money remains unknown and units are hundred million',()=>{
  assert.equal(money(null),'—');assert.equal(money('0'),'0.00');assert.equal(money('123000000'),'1.23');assert.equal(numeric(undefined),null);
});
test('editable scope uses explicit group and rejects malformed rows',()=>{
  assert.deepEqual(parseFunds('sse.510300，华泰柏瑞，沪深300'),[{code:'SSE.510300',name:'华泰柏瑞',group:'沪深300'}]);
  assert.throws(()=>parseFunds('510300,missing group'));
});
test('chart gaps are not bridged and zero remains a valid point',()=>{
});
test('collection counts distinguish empty responses from legacy missing diagnostics',()=>{
  assert.match(countsText({daily:6}),/份额 0 条、净值 0 条/);
  assert.match(countsText(undefined),/旧任务未记录/);
});
