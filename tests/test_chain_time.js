const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../static/chain-time.js');
const thresholds = {prepare:120,attack:60,urgent:30,critical:15};
test('timer deducts observation age and synchronizes to new deadline',()=>{
  assert.equal(T.remaining(1000+60,1020),40);
  assert.equal(T.remaining(1020+300,1020),300);
  assert.equal(T.remaining(1000,1100),0);
  for(const invalid of [null,undefined,NaN,Infinity,-1,'100']) assert.equal(T.remaining(invalid,1000),null);
});
test('advisory warning boundaries',()=>{
  for(const [n,label] of [[121,'Normal'],[120,'Prepare'],[61,'Prepare'],[60,'Attack window'],[31,'Attack window'],[30,'Urgent'],[16,'Urgent'],[15,'Critical'],[1,'Critical']]) assert.equal(T.warning(n,thresholds),label);
  assert.match(T.warning(0,thresholds),/awaiting confirmation/);
});
test('hospital expiry remains unconfirmed until new API status',()=>{
  const m={status:'Hospital',hospital_until:1000};
  assert.equal(T.hospital(m,999),'Hospital · estimated release in 0:01');
  assert.equal(T.hospital(m,1001),'Expected Release — Awaiting Confirmation');
  assert.match(T.hospital({status:'Hospital'},1000),/unavailable/);
  assert.equal(T.hospital({status:'Okay'},1001),'');
});
test('cached or failed observations remain stale',()=>{
  assert.equal(T.stale({observed_at:1000},1100,120),false);
  assert.equal(T.stale({observed_at:1000},1201,120),true);
  assert.equal(T.stale({observed_at:1000,error:'offline'},1001,120),true);
  assert.equal(T.stale({},1000,120),true);
});
