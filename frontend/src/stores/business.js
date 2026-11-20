import {batches,inspections,reviews,index} from '../api/business.js';
export const state={user:null,batches:[],inspections:[],reviews:[],selected:null,indexStatus:null,indexWindow:null,indexLine:''};
export async function refresh(){
  [state.batches,state.inspections,state.reviews,state.indexStatus]=await Promise.all([batches.list(),inspections.list(),reviews.list(),index.status()]);
  try{state.indexWindow=await index.window(state.indexLine,state.indexStatus?.window_hours||24);}catch(e){state.indexWindow=null;}
  if(state.selected)state.selected=await reviews.get(state.selected.id);
}
export async function refreshIndex(){
  state.indexStatus=await index.status();
  try{state.indexWindow=await index.window(state.indexLine,state.indexStatus?.window_hours||24);}catch(e){state.indexWindow=null;}
}
