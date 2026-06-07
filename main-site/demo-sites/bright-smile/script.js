const navToggle=document.querySelector(".nav-toggle");
const navMenu=document.querySelector(".nav-menu");
if(navToggle&&navMenu){navToggle.addEventListener("click",()=>{const o=navMenu.classList.toggle("open");navToggle.setAttribute("aria-expanded",String(o));});navMenu.querySelectorAll("a").forEach(a=>a.addEventListener("click",()=>{navMenu.classList.remove("open");navToggle.setAttribute("aria-expanded","false");}));}
const year=document.getElementById("year");if(year)year.textContent=new Date().getFullYear();
const form=document.querySelector("[data-demo-form]");if(form){form.addEventListener("submit",e=>{e.preventDefault();const note=document.querySelector("[data-form-note]");if(note)note.textContent="Demo only: this form is ready to connect to Mia, a booking link, or your backend later.";});}
