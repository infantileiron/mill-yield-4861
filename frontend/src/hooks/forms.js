let pending=null;
let timer=null;
export function formData(form,numeric=[]){const out=Object.fromEntries(new FormData(form));for(const k of numeric)out[k]=Number(out[k]);return out;}
export function notify(message,error=false){
  clearTimeout(timer);
  pending=message?{message,error}:null;
  paint();
  if(message)timer=setTimeout(()=>{pending=null;paint();},9000);
}
// Re-applied after every re-render so background polling does not wipe the message.
export function paint(){
  const el=document.getElementById('notice');
  if(!el)return;
  if(!pending){el.hidden=true;el.textContent='';return;}
  el.textContent=pending.message;
  el.className=pending.error?'notice error':'notice';
  el.hidden=false;
}
