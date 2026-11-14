export type Offer = { id:string; seller:string; price:number; shipping:number; available:boolean; matches:boolean; note?:string };
export type Card = {id:string;name:string;japanese:string;image:string;rarity:string;type:string;color:string;reference:number;growth:number;days:number;history:number[];offers:Offer[];catalogUrl:string};
export type Settings={budget:number;reserve:number;horizon:number;risk:string;scenario:string};
export const feeRate=.10;
export const exitShipping=250;
export function offersFor(c:Card){return c.offers.filter(o=>o.available&&o.matches).sort((a,b)=>(a.price+a.shipping)-(b.price+b.shipping));}
export function quote(c:Card,s:Settings){
 const offer=offersFor(c)[0];const cost=offer?offer.price+offer.shipping:Infinity;
 const adjustment=s.scenario==='bear'?-0.15:s.scenario==='bull'?.10:0;
 const exit=Math.round(c.reference*(1+c.growth*Math.min(s.horizon,90)/30)*(1+adjustment));
 const proceeds=Math.floor(exit*(1-feeRate))-exitShipping;
 return {offer,cost,exit,proceeds,profit:proceeds-cost,roi:cost===Infinity?0:(proceeds-cost)/cost,breakEven:Math.ceil((cost+exitShipping)/(1-feeRate)),eligible:c.days<=s.horizon};
}
export function optimize(cards:Card[],s:Settings,mode:'investor'|'collector',wish:string[]=[]){
 const spendable=Math.max(0,Math.floor(s.budget-s.reserve));
 const items=cards.filter(c=>mode==='investor'||wish.includes(c.id)).map(c=>({card:c,...quote(c,s)})).filter(x=>Number.isFinite(x.cost)&&(mode==='collector'||(x.profit>0&&x.eligible)));
 const limit=s.risk==='careful'?.45:s.risk==='balanced'?.7:1;
 let best:{items:typeof items;cost:number;profit:number;score:number}={items:[],cost:0,profit:0,score:0};
 for(let mask=1;mask<(1<<items.length);mask++){
  const picked=items.filter((_,i)=>mask&(1<<i));
  const cost=picked.reduce((a,x)=>a+x.cost,0);if(cost>spendable)continue;
  if(mode==='investor'&&picked.some(x=>x.cost>spendable*limit))continue;
  const profit=picked.reduce((a,x)=>a+x.profit,0);const score=mode==='collector'?picked.length:profit;
  if(score>best.score||(score===best.score&&cost<best.cost))best={items:picked,cost,profit,score};
 }
 return {...best,unspent:s.budget-best.cost,spendable,missing:mode==='collector'?wish.filter(id=>!best.items.some(x=>x.card.id===id)):[]};
}
export function marketEvent(cards:Card[],event:string,tick:number):Card[]{
 return cards.map((c,i)=>{
  const factor=event==='restock'?(i<4?.85:1):event==='demand'?(i%3===0?1.12:1):(1+(((tick+i*3)%7)-3)*.012);
  return {...c,reference:Math.round(c.reference*factor),offers:c.offers.map((o,j)=>({...o,price:Math.round(o.price*(event==='soldout'?1:factor*.99+.01)),available:event==='soldout'&&j===0&&i%2===0?false:o.available})),history:[...c.history.slice(1),Math.round(c.reference*factor)]};
 });
}
export function buyPaper(wallet:number,card:Card,s:Settings){const q=quote(card,s);if(!q.offer)throw Error('No matching offer is available.');if(q.cost>wallet)throw Error('Not enough paper cash. Sell a holding or reset the demo.');return {wallet:wallet-q.cost,holding:{id:crypto.randomUUID(),cardId:card.id,cost:q.cost,boughtAt:new Date().toISOString()}};}
export function sellPaper(wallet:number,holding:{cost:number},card:Card){const proceeds=Math.floor(card.reference*(1-feeRate))-exitShipping;return {wallet:wallet+proceeds,proceeds,realized:proceeds-holding.cost};}
