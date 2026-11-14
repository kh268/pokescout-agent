import catalog from './card-catalog.json';
import type {Card} from './engine';
const specs:Record<string,[number,number,number,number,string,string]>={
 'SV2a-205':[16800,20800,.025,14,'Psychic','#f9e4f0'],
 'SV2a-201':[24600,28700,.035,21,'Fire','#ffecd8'],
 'SV2a-173':[4200,5500,.02,7,'Electric','#fff2b9'],
 'SV2a-166':[1800,2600,.012,7,'Grass','#dff4e9'],
 'SV2a-170':[2100,3000,.018,10,'Water','#e0effb'],
 'SV2a-168':[2400,3400,.022,7,'Fire','#ffe6db'],
 'SV2a-203':[6300,8200,.018,14,'Psychic','#eee5fb'],
 'SV2a-204':[7200,9100,.015,14,'Electric','#fff1c8'],
 'SV2a-200':[9800,12100,.01,21,'Grass','#e1efdb'],
 'SV2a-202':[10200,12600,.012,21,'Water','#dcecf8']};
export function initialMarket():Card[]{return catalog.map((c,i)=>{const [ask,reference,growth,days,type,color]=specs[c.id];return {
 id:c.id,name:c.nameEnglish,japanese:c.nameJapanese,image:c.imageLocal,rarity:c.id.endsWith('173')||['166','168','170'].some(n=>c.id.endsWith(n))?'AR':'SAR',type,color,reference,growth,days,catalogUrl:c.sourceUrl,
 history:Array.from({length:20},(_,j)=>Math.round(reference*(.87+j*.006+Math.sin(j*1.3+i)*.025))),
 offers:[{id:c.id+'-a',seller:'Mercari',price:ask,shipping:0,available:true,matches:true,note:'Sample offer · Japanese · raw near mint'},
 {id:c.id+'-b',seller:'Tokyo Card Shop',price:Math.round(ask*1.035),shipping:250,available:true,matches:true,note:'Sample offer · Japanese · raw near mint'},
 {id:c.id+'-c',seller:'Community Market',price:Math.round(ask*.74),shipping:180,available:true,matches:false,note:'Excluded: a different printing / condition'}]
 };});}
