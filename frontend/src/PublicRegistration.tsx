import {useEffect, useRef, useState} from 'react';

type Registration = {id:number;name:string;starts_at:number|null;open:boolean;reason:string;instagram:string};
type Attempt = {requestId:string;opened:boolean;checked?:boolean;ticketUrl?:string};
const readAttempt=(key:string):Attempt=>{
  try {const raw=JSON.parse(sessionStorage.getItem(key)||'null');if(raw?.requestId)return raw;}catch{}
  return {requestId:crypto.randomUUID(),opened:false};
};

export default function PublicRegistration(){
  const eventId=location.pathname.split('/')[2];
  const storageKey='event-pass:registration:'+eventId;
  const [attempt,setAttempt]=useState<Attempt>(()=>readAttempt(storageKey));
  const [event,setEvent]=useState<Registration|null>(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const locked=useRef(false);
  const update=(a:Attempt)=>{setAttempt(a);try{sessionStorage.setItem(storageKey,JSON.stringify(a));}catch{}};
  useEffect(()=>{
    let alive=true;
    fetch('/api/registration/'+eventId).then(async r=>{const data=await r.json();if(!r.ok)throw Error(data.detail||'Не удалось открыть мероприятие');return data;}).then(d=>{if(alive)setEvent(d);}).catch(e=>{if(alive)setError(e.message);});
    return()=>{alive=false;};
  },[eventId]);
  const checking=!!event?.instagram&&attempt.opened&&!attempt.checked;
  useEffect(()=>{
    if(!checking)return;
    let timer:ReturnType<typeof setTimeout>|undefined;
    const start=()=>{
      if(timer)clearTimeout(timer);
      if(document.visibilityState==='visible'&&document.hasFocus()){
        timer=setTimeout(()=>update({...attempt,checked:true}),2500);
      }
    };
    start();
    window.addEventListener('focus',start);
    window.addEventListener('blur',start);
    document.addEventListener('visibilitychange',start);
    return()=>{if(timer)clearTimeout(timer);window.removeEventListener('focus',start);window.removeEventListener('blur',start);document.removeEventListener('visibilitychange',start);};
  },[checking,attempt]);
  async function issue(){
    if(locked.current||checking||!!event?.instagram&&!attempt.checked)return;
    locked.current=true;setBusy(true);setError('');
    // Persist before sending so a retry after navigation keeps the same request ID.
    update(attempt);
    try {
      const r=await fetch('/api/registration/'+eventId+'/tickets',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'EventPass'},body:JSON.stringify({request_id:attempt.requestId})});
      const data=await r.json();
      if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'Не удалось создать билет');
      update({...attempt,ticketUrl:data.url});
      location.assign(data.url);
    }catch(e){setError(e instanceof TypeError?'Нет связи с сервером. Попробуйте ещё раз — повторный запрос не создаст лишний билет.':(e as Error).message);}
    finally{setBusy(false);locked.current=false;}
  }
  return <main className="ticket-wrap"><div className="brand">▣ EVENT PASS</div><article className="pass registration">
    <p className="eyebrow">ВАШ ПРОПУСК НА СОБЫТИЕ</p>
    <h1>{event?.name||'Получить билет'}</h1>
    {event?.starts_at&&<p>{new Date(event.starts_at*1000).toLocaleString('ru-RU')}</p>}
    {error&&<p className="error" role="alert">{error}</p>}
    {!event&&!error&&<p>Загружаем мероприятие…</p>}
    {event&&attempt.ticketUrl?<>
      <p>Ваш билет уже готов. Откройте его и покажите QR на входе.</p>
      <a className="button registration-action" href={attempt.ticketUrl}>Открыть мой билет →</a>
    </>:event&&!event.open?<p className="notice">{event.reason}</p>:event?<>
      <p>Без анкеты и входа в аккаунт. Получите QR и покажите его сотруднику на входе.</p>
      {event.instagram&&<section className="registration-step">
        <span className="step-number">1</span><div><h2>Загляните в наш Instagram</h2><p>Подпишитесь на @{event.instagram}, чтобы быть в курсе событий, и вернитесь сюда.</p>
        <a className="button registration-action secondary" href={'https://www.instagram.com/'+encodeURIComponent(event.instagram)+'/'} target="_blank" rel="noopener noreferrer" onClick={()=>update({...attempt,opened:true,checked:false})}>Открыть Instagram ↗</a></div>
      </section>}
      <section className="registration-step"><span className="step-number">{event.instagram?'2':'1'}</span><div><h2>Заберите свой QR</h2><p>Каждый билет действует на один проход.</p>
        <button className="registration-action" disabled={busy||!!event.instagram&&!attempt.checked} aria-busy={busy||checking} onClick={issue}>{checking?'Проверяем переход…':busy?'Создаём ваш билет…':'Получить билет →'}</button>
      </div></section>
      <small>Подписка добровольная. Переход в Instagram не подтверждает подписку автоматически.</small>
    </>:null}
  </article><small className="registration-footer">Сохраните ссылку на билет или сделайте скриншот QR.</small></main>;
}
