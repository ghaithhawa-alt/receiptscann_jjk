// Landing Page FAQ + Smooth Scroll
document.querySelectorAll('.faq-q').forEach(function(q){
  q.addEventListener('click', function(){ this.parentElement.classList.toggle('open'); });
});
document.querySelectorAll('a[href^="#"]').forEach(function(a){
  a.addEventListener('click', function(e){
    var t = document.querySelector(this.getAttribute('href'));
    if(t){ e.preventDefault(); t.scrollIntoView({behavior:'smooth'}); }
  });
});
