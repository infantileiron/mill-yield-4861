import {api,post,download} from './client.js';
export const batches={list:()=>api('/batches'),create:d=>post('/batches',d)};
export const inspections={list:()=>api('/inspections'),create:d=>post('/inspections',d)};
export const reviews={list:()=>api('/reviews'),get:id=>api('/reviews/'+id),create:d=>post('/reviews',d),createIndexed:d=>post('/reviews/indexed',d),approve:(id,note)=>post(`/reviews/${id}/approve`,{note}),download};
function query(params){const q=new URLSearchParams();for(const [k,v] of Object.entries(params))if(v!==undefined&&v!==null&&v!=='')q.set(k,v);const s=q.toString();return s?'?'+s:'';}
export const index={
  status:()=>api('/index/status'),
  build:hours=>post('/index/build',{window_hours:hours||24}),
  sync:()=>post('/index/sync'),
  window:(line,hours)=>api('/index/window'+query({line,hours})),
};
