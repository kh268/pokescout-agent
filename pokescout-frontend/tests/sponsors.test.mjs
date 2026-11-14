import assert from 'node:assert/strict';
import {test} from 'node:test';
import {sponsorRequest,executeSponsor,redact} from '../lib/sponsors.ts';
const input=(provider='daytona')=>sponsorRequest.parse({provider,config:{},cards:[{id:'SV2a-173',cost:4000,profit:500}],budget:5000,horizon:30});
test('unconfigured providers never produce simulated live success',async()=>{
 for(const provider of ['tavily','nosana','daytona','codeaf'])await assert.rejects(executeSponsor(input(provider),{}),/Not configured/);
});
test('input validation excludes injected IDs and nonfinite or excessive values',()=>{
 const d=input();d.cards[0].id='../../secret';assert.equal(sponsorRequest.safeParse(d).success,false);
 d.cards[0].id='SV2a-173';d.cards[0].cost=Infinity;assert.equal(sponsorRequest.safeParse(d).success,false);
});
test('secrets are redacted recursively',()=>{assert.deepEqual(redact({nested:{output:'key mock-secret here'}},['mock-secret']),{nested:{output:'key [REDACTED] here'}})});
test('sandbox accounting rejects malformed output and still requests cleanup',async(t)=>{
 for(const answer of [{ids:['SV2a-173']},{ids:['SV2a-173'],cost:1,profit:999},{ids:['SV2a-999'],cost:4000,profit:500}]){
  const calls=[];
  const m=t.mock.method(globalThis,'fetch',async(url,init)=>{calls.push({url,method:init.method});if(init.method==='DELETE')return new Response(null,{status:204});if(String(url).endsWith('/process/code-run'))return Response.json({exitCode:0,result:JSON.stringify(answer)});return Response.json({id:'sandbox-fixture',state:'started',toolboxProxyUrl:'https://proxy.app.daytona.io/toolbox'});});
  await assert.rejects(executeSponsor(input(),{DAYTONA_API_KEY:'test-only-value'}),/Invalid optimizer|accounting checks|unknown card/);
  assert.equal(calls.at(-1).method,'DELETE');m.mock.restore();
 }
});
test('valid sandbox result is returned only after budget accounting check',async(t)=>{
 t.mock.method(globalThis,'fetch',async(url,init)=>{if(init.method==='DELETE')return new Response(null,{status:204});if(String(url).endsWith('/process/code-run'))return Response.json({exitCode:0,result:JSON.stringify({ids:['SV2a-173'],cost:4000,profit:500})});return Response.json({id:'sandbox-fixture',state:'started',toolboxProxyUrl:'https://proxy.app.daytona.io/toolbox'});});
 const result=await executeSponsor(input(),{DAYTONA_API_KEY:'test-only-value'});assert.equal(result.result.cost,4000);assert.match(result.cleanup,/successfully/);
});
