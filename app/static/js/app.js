// The Find Brief — client-side JS
// HTMX handles most interactivity. This file is for anything HTMX can't cover.

// Disable the Generate button after click to prevent double-triggers
document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (e.detail.elt.id === 'generate-btn') {
        e.detail.elt.disabled = true;
        e.detail.elt.classList.add('btn--disabled');
        e.detail.elt.textContent = 'Generating...';
    }
});

// ---- Compliance Popover Logic ----

(function () {
    var activePopover = null;

    document.body.addEventListener('click', function (e) {
        var indicator = e.target.closest('.compliance-indicator');
        var highlight = e.target.closest('.compliance-highlight');
        var target = indicator || highlight;

        if (target) {
            e.stopPropagation();
            var flagId = target.getAttribute('data-flag-id');
            if (!flagId) return;

            var popover = document.getElementById('popover-' + flagId);
            if (!popover) return;

            // Close previous popover
            if (activePopover && activePopover !== popover) {
                activePopover.classList.remove('compliance-popover--visible');
            }

            // Toggle this popover
            if (popover.classList.contains('compliance-popover--visible')) {
                popover.classList.remove('compliance-popover--visible');
                activePopover = null;
                return;
            }

            // Position the popover near the clicked element
            var section = target.closest('.draft-section');
            var popoversContainer = section ? section.querySelector('.compliance-popovers') : null;

            if (popoversContainer) {
                var containerRect = popoversContainer.getBoundingClientRect();
                var targetRect = target.getBoundingClientRect();

                var top = targetRect.bottom - containerRect.top + 8;
                var left = targetRect.left - containerRect.left;

                // Keep within viewport horizontally
                var viewportWidth = window.innerWidth;
                if (left + 360 > viewportWidth - containerRect.left) {
                    left = Math.max(0, viewportWidth - containerRect.left - 376);
                }

                popover.style.top = top + 'px';
                popover.style.left = left + 'px';
            }

            popover.classList.add('compliance-popover--visible');
            activePopover = popover;
            return;
        }

        // Click outside — close active popover (but not if clicking inside a popover)
        if (activePopover && !e.target.closest('.compliance-popover')) {
            activePopover.classList.remove('compliance-popover--visible');
            activePopover = null;
        }
    });

    // Escape key closes active popover
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && activePopover) {
            activePopover.classList.remove('compliance-popover--visible');
            activePopover = null;
        }
    });
})();
