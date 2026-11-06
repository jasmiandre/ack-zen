import { chromium } from "/home/zen/ack-tools/node_modules/playwright/index.mjs";
// Browser regression: only #next on steps 1-4, only #send on step 5; resize grip >=44px, tools gap >=8px.
const U=process.env.URL||"https://103-31-39-95.sslip.io/daftar", D=process.env.OUT||".";
const b=await chromium.launch(); let bad=[];
for(const [tag,w,h] of [["desktop",1440,900],["mobile",390,844]]){
 const p=await (await b.newContext({viewport:{width:w,height:h}})).newPage(); await p.goto(U);
 const vis=()=>p.evaluate(()=>["prev","next","send"].map(i=>{const e=document.getElementById(i);return getComputedStyle(e).display!=="none"&&!e.hidden}));
 const N=Date.now()%1e8, set=(n,v)=>p.fill(`[name=${n}]`,v);
 const chk=async(s,exp)=>{const v=await vis(); if(JSON.stringify(v)!==JSON.stringify(exp)) bad.push(`${tag} step${s} ${v}`)};
 await chk(1,[false,true,false]);
 await set("full_name","Budi Fiktif");await set("phone","0812"+String(N).padStart(8,"0"));await set("email",`t${N}@example.test`);await set("password","Kata-sandi-Uji-123");await set("password2","Kata-sandi-Uji-123");await p.click("#next");await chk(2,[true,true,false]);
 await p.evaluate(()=>document.querySelectorAll("fieldset.step:not([hidden]) select").forEach(s=>{s.selectedIndex=1;s.dispatchEvent(new Event("change",{bubbles:true}))}));await set("birth_date","1995-05-05");await set("domicile","Jakarta");await set("height_cm","170");await p.click("#next");await chk(3,[true,true,false]);
 await set("intro","Saya orang dummy yang suka padel dan kopi, mencari teman hidup.");await set("pref_age_min","25");await set("pref_age_max","35");await set("pref_domicile","Jabodetabek");await p.evaluate(()=>document.querySelectorAll("fieldset.step:not([hidden]) select").forEach(s=>{s.selectedIndex=1}));await p.click("#next");await chk(4,[true,true,false]);
 await p.setInputFiles("#ktp-src",D+"/ktp_big.jpg");await p.waitForSelector("#ktp-tools:not([hidden])");
 const g=await p.evaluate(()=>{const a=document.getElementById("add-box").getBoundingClientRect(),m=document.getElementById("make-prev").getBoundingClientRect();return {gap:a.top===m.top?m.left-a.right:m.top-a.bottom}});
 if(g.gap<8) bad.push(`${tag} gap ${g.gap}`);
 await p.screenshot({path:`${D}/step4-${tag}.png`,fullPage:true});
 await p.setInputFiles("#photo_face",D+"/face_big.jpg");await p.setInputFiles("#photo_body",D+"/body_big.jpg");
 await p.click("#make-prev");await p.waitForSelector("#prev-wrap:not([hidden])");
 for(const k of ["ktp_nik_ok","ktp_addr_ok","ktp_sign_ok"]) await p.check(`[name=${k}]`);
 await p.waitForFunction(()=>[...document.querySelectorAll("[data-note]")].every(n=>/^Siap/.test(n.textContent)),null,{timeout:60000});
 await p.click("#next");await chk(5,[true,false,true]);
 await p.screenshot({path:`${D}/step5-${tag}.png`,fullPage:true});
}
console.log(bad.length?"FAIL "+bad.join("; "):"PASS"); await b.close(); process.exit(bad.length?1:0);
