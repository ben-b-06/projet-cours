/* Comportements communs. Aucun script en ligne dans les pages : la politique
   de sécurité du contenu (CSP) n'autorise que les fichiers de /static. */
(function () {
  'use strict';
  document.documentElement.classList.add('js');

  function syncConditional(form) {
    form.querySelectorAll('[data-show-when]').forEach(function (el) {
      var parts = el.getAttribute('data-show-when').split(':');
      var field = form.elements[parts[0]];
      var value = field ? field.value : '';
      var show = parts[1].split(',').indexOf(value) !== -1;
      el.hidden = !show;
      if (el.hasAttribute('data-required-when-shown')) {
        el.querySelectorAll('input, select, textarea').forEach(function (input) { input.required = show; });
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var header = document.querySelector('.site-header');
    var menuButton = document.querySelector('.menu-button');
    if (header && menuButton) {
      menuButton.addEventListener('click', function () {
        var open = header.classList.toggle('is-open');
        menuButton.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    }

    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (event) {
        if (!window.confirm(form.getAttribute('data-confirm'))) {
          event.preventDefault();
        }
      });
    });

    document.querySelectorAll('form').forEach(function (form) {
      if (form.querySelector('[data-show-when]')) {
        form.addEventListener('change', function () { syncConditional(form); });
        syncConditional(form);
      }
    });

    // Création d'un cours : ajout et retrait de lignes de créneaux.
    var rows = document.getElementById('slotRows');
    var template = document.getElementById('slotRowTemplate');
    var addButton = document.getElementById('addSlotBtn');
    if (rows && template && addButton) {
      var addRow = function () {
        var node = template.content.cloneNode(true);
        node.querySelector('.slot-remove').addEventListener('click', function (event) {
          event.target.closest('.slot-row').remove();
        });
        rows.appendChild(node);
      };
      addButton.addEventListener('click', addRow);
      if (!rows.querySelector('.slot-row')) { addRow(); }
    }

    // Messagerie : nom du fichier joint, conversation affichée en bas.
    document.querySelectorAll('input[type="file"][data-filename]').forEach(function (input) {
      var target = document.getElementById(input.getAttribute('data-filename'));
      input.addEventListener('change', function () {
        if (target) { target.textContent = input.files[0] ? input.files[0].name : ''; }
      });
    });
    var thread = document.querySelector('.conversation .thread');
    if (thread) { thread.scrollTop = thread.scrollHeight; }
  });
})();
